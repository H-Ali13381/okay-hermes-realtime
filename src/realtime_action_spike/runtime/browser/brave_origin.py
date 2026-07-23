from __future__ import annotations

import contextlib
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

    def release_profile_lock(self) -> None:
        """Release this profile's singleton lock after the browser has exited.

        Safe to call from teardown regardless of how the browser stopped; a live
        owner is spared and an absent lock is a no-op.
        """
        release_singleton_lock(self._brave_profile)

    def _build_loopback_url(self, activation_token: str) -> str:
        return build_activation_loopback_url(self._loopback_base_url, activation_token)

    def launch(self, loopback_url: str) -> DedicatedBrowserHandle:
        resolved_loopback_url = validate_activation_loopback_url(
            loopback_url,
            self._loopback_base_url,
        )
        _ensure_dedicated_profile(self._brave_profile)
        _clear_stale_singleton_lock(self._brave_profile)

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


_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_singleton_lock(profile_path: Path) -> None:
    """Remove Chromium singleton lock files left by a dead owner.

    Chromium writes ``SingletonLock`` as a symlink of the form ``<host>-<pid>``.
    When a prior dedicated browser dies without cleaning up, the stale lock makes
    the next launch hand the URL to a nonexistent instance and exit immediately,
    so the controller only ever sees a browser-startup timeout. Clear the lock
    only when its owning PID is dead; refuse to launch if a live instance owns it.
    """

    if _singleton_lock_owner_is_alive(profile_path):
        raise BrowserLaunchError("dedicated Brave profile is already in use by a live instance")
    _unlink_singleton_lock_files(profile_path)


def release_singleton_lock(profile_path: Path) -> None:
    """Release this profile's singleton lock during teardown.

    Mirrors :func:`_clear_stale_singleton_lock` but is safe to call after the
    dedicated browser process group has exited: it removes the lock files so the
    profile never goes stale, while sparing a lock that a live instance still
    owns (a hard-kill race) and staying a no-op when no lock is present.
    """

    if _singleton_lock_owner_is_alive(profile_path):
        return
    _unlink_singleton_lock_files(profile_path)


def _singleton_lock_owner_is_alive(profile_path: Path) -> bool:
    lock_path = profile_path / "SingletonLock"
    if not lock_path.is_symlink():
        return False
    owner_pid = _parse_singleton_lock_pid(os.readlink(lock_path))
    return owner_pid is not None and _pid_is_alive(owner_pid)


def _unlink_singleton_lock_files(profile_path: Path) -> None:
    for name in _SINGLETON_LOCK_FILES:
        candidate = profile_path / name
        if candidate.is_symlink() or candidate.exists():
            with contextlib.suppress(OSError):
                candidate.unlink()


def _parse_singleton_lock_pid(target: str) -> int | None:
    _, separator, pid_text = target.rpartition("-")
    if not separator or not pid_text.isdigit():
        return None
    return int(pid_text)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
