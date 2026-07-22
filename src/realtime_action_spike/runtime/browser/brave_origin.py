from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from .contracts import BrowserLaunchError
from .process import DedicatedBrowserHandle, KillProcessGroup
from .urls import (
    build_activation_loopback_url,
    validate_activation_loopback_url,
    validate_loopback_base_url,
)

ProcessFactory = Callable[..., subprocess.Popen]
_BRAVE_CLOSE_TIMEOUT_SECONDS = 0.6


class DedicatedBraveLauncher:
    """Launch Brave Origin Nightly with exact process ownership semantics."""

    def __init__(
        self,
        brave_binary: str,
        brave_profile: str,
        loopback_base_url: str,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        kill_process_group: KillProcessGroup = os.killpg,
    ) -> None:
        self._resolved_brave_binary = _resolve_executable(brave_binary)
        self._brave_profile = _expand_profile_path(brave_profile)
        self._loopback_base_url = validate_loopback_base_url(loopback_base_url)
        self._process_factory = process_factory
        self._kill_process_group = kill_process_group

    @property
    def resolved_brave_binary(self) -> str:
        return self._resolved_brave_binary

    @property
    def brave_profile(self) -> Path:
        return self._brave_profile

    def _build_loopback_url(self, activation_token: str) -> str:
        return build_activation_loopback_url(self._loopback_base_url, activation_token)

    def launch(self, loopback_url: str) -> DedicatedBrowserHandle:
        resolved_loopback_url = validate_activation_loopback_url(
            loopback_url,
            self._loopback_base_url,
        )
        _ensure_dedicated_profile(self._brave_profile)

        args = [
            self._resolved_brave_binary,
            f"--user-data-dir={self._brave_profile}",
            f"--app={resolved_loopback_url}",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-background-mode",
            # This isolated profile opens only the loopback voice page. Auto-accept
            # its microphone request so wake activation cannot block on a prompt.
            "--use-fake-ui-for-media-stream",
        ]
        process_options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "start_new_session": True,
        }
        vendor_environment = _brave_origin_environment(self._resolved_brave_binary)
        if vendor_environment is not None:
            process_options["env"] = vendor_environment
        process = self._process_factory(args, **process_options)

        if process.poll() is not None:
            raise BrowserLaunchError("Brave process exited immediately after startup request")

        return DedicatedBrowserHandle(
            process=process,
            process_group_id=process.pid,
            close_timeout_seconds=_BRAVE_CLOSE_TIMEOUT_SECONDS,
            kill_process_group=self._kill_process_group,
        )


def _brave_origin_environment(binary: str) -> dict[str, str] | None:
    """Restore vendor channel identity while directly owning the real binary."""

    executable = Path(binary)
    wrapper = executable.with_name("brave-origin")
    if executable.name != "brave" or not wrapper.is_file():
        return None

    environment = os.environ.copy()
    environment.update(
        {
            "CHROME_WRAPPER": str(wrapper.resolve()),
            "CHROME_VERSION_EXTRA": "nightly",
            "GNOME_DISABLE_CRASH_DIALOG": "SET_BY_GOOGLE_CHROME",
        }
    )
    return environment


def _resolve_executable(binary: str) -> str:
    candidate_path = Path(os.path.expanduser(binary))
    if not candidate_path.is_absolute():
        resolved_path = shutil.which(str(candidate_path))
        if resolved_path is None:
            raise BrowserLaunchError(f"Brave binary is not configured or not found: {binary}")
        candidate_path = Path(resolved_path)

    if not candidate_path.is_file() or not os.access(candidate_path, os.X_OK):
        raise BrowserLaunchError(f"Brave binary is not executable: {candidate_path}")

    return str(candidate_path.resolve())


def _expand_profile_path(profile_path: str) -> Path:
    return Path(os.path.expanduser(profile_path))


def _ensure_dedicated_profile(profile_path: Path) -> None:
    try:
        profile_path.mkdir(parents=True, exist_ok=True)
        profile_path.chmod(stat.S_IRWXU)
    except OSError as exc:
        raise BrowserLaunchError(f"could not prepare Brave profile directory: {exc}") from exc
