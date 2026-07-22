from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class BrowserLaunchError(RuntimeError):
    """Raised when Brave cannot be launched or closed cleanly."""


class BrowserHandle(Protocol):
    """Minimal protocol for browser handles."""

    def close(self) -> None: ...


class NoopBrowserHandle:
    """Explicit non-allocating handle used by diagnostics and tests."""

    def __init__(self) -> None:
        self.closed_calls = 0

    def close(self) -> None:
        self.closed_calls += 1


ProcessFactory = Callable[..., subprocess.Popen]
KillProcessGroup = Callable[[int, int], None]
ProcessGroupExists = Callable[[int], bool]
Clock = Callable[[], float]
Sleep = Callable[[float], None]
_BROWSER_CLOSE_PHASE_TIMEOUT_SECONDS = 0.6


class DedicatedBrowserHandle:
    """Own one Brave process and tear it down via its own process group."""

    def __init__(
        self,
        process: subprocess.Popen,
        process_group_id: int,
        close_timeout_seconds: float,
        *,
        kill_process_group: KillProcessGroup = os.killpg,
        process_group_exists: ProcessGroupExists | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._process = process
        self.process_group_id = process_group_id
        self.close_timeout_seconds = close_timeout_seconds
        self._kill_process_group = kill_process_group
        self._process_group_exists = process_group_exists or _process_group_exists
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return

        deadline = self._clock() + (self.close_timeout_seconds * 3)
        try:
            if self._process.poll() is not None:
                self._ensure_owned_process_group_exited(deadline)
                self._closed = True
                return

            # The page gets the first chance to close itself after media cleanup.
            try:
                self._process.wait(timeout=self._phase_timeout(deadline))
                self._ensure_owned_process_group_exited(deadline)
                self._closed = True
                return
            except subprocess.TimeoutExpired:
                pass

            # TERM only the owned browser main PID. Sending TERM to every Chromium
            # child at once causes Brave/Crashpad to report a SIGTRAP crash.
            self._process.terminate()
            try:
                self._process.wait(timeout=self._phase_timeout(deadline))
            except subprocess.TimeoutExpired:
                # A stuck tree gets hard-killed without generating a crash core.
                self._kill_process_group(self.process_group_id, signal.SIGKILL)
                try:
                    self._process.wait(timeout=self._phase_timeout(deadline))
                except subprocess.TimeoutExpired as kill_exc:
                    raise BrowserLaunchError(
                        "browser process group did not exit after KILL"
                    ) from kill_exc

            self._ensure_owned_process_group_exited(deadline)
            self._closed = True
        except Exception as exc:
            raise BrowserLaunchError(
                f"failed to close Brave process group: {type(exc).__name__}"
            ) from exc

    def _phase_timeout(self, deadline: float) -> float:
        return min(
            self.close_timeout_seconds,
            max(0.001, deadline - self._clock()),
        )

    def _ensure_owned_process_group_exited(self, deadline: float) -> None:
        if not self._process_group_exists(self.process_group_id):
            return

        self._kill_process_group(self.process_group_id, signal.SIGKILL)
        while self._process_group_exists(self.process_group_id):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RuntimeError("owned Brave process group did not exit")
            self._sleep(min(0.01, remaining))


class DedicatedBraveLauncher:
    """Launch Brave in app-mode with exact process ownership semantics."""

    def __init__(
        self,
        brave_binary: str,
        brave_profile: str,
        loopback_base_url: str,
        start_timeout_seconds: float,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        kill_process_group: KillProcessGroup = os.killpg,
        sleep: Sleep | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._resolved_brave_binary = _resolve_executable(brave_binary)
        self._brave_profile = _expand_profile_path(brave_profile)
        self._loopback_base_url = _validate_loopback_base_url(loopback_base_url)
        self._start_timeout_seconds = float(start_timeout_seconds)

        self._process_factory = process_factory
        self._kill_process_group = kill_process_group
        self._sleep = sleep
        self._clock = clock

    @property
    def resolved_brave_binary(self) -> str:
        return self._resolved_brave_binary

    @property
    def brave_profile(self) -> Path:
        return self._brave_profile

    def _build_loopback_url(self, activation_token: str) -> str:
        parsed = urlsplit(self._loopback_base_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["activation"] = [activation_token]
        encoded = urlencode(query, doseq=True)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, ""))

    def launch(self, loopback_url: str) -> DedicatedBrowserHandle:
        resolved_loopback_url = _validate_activation_loopback_url(
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
            # This isolated app profile opens only the loopback voice page. Auto-accept
            # its real microphone request so wake activation never depends on a hidden
            # browser permission prompt.
            "--use-fake-ui-for-media-stream",
        ]
        process = self._process_factory(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )

        if process.poll() is not None:
            raise BrowserLaunchError("Brave process exited immediately after startup request")

        return DedicatedBrowserHandle(
            process=process,
            process_group_id=process.pid,
            close_timeout_seconds=_BROWSER_CLOSE_PHASE_TIMEOUT_SECONDS,
            kill_process_group=self._kill_process_group,
        )


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


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _validate_loopback_base_url(url: str) -> str:
    parsed = _parse_loopback_url(url)
    if parsed.query:
        raise BrowserLaunchError("loopback base URL must not include query parameters")
    if parsed.fragment:
        raise BrowserLaunchError("loopback base URL must not include a fragment")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserLaunchError("loopback base URL must not include credentials")
    if not _is_loopback_host(parsed.hostname):
        raise BrowserLaunchError("loopback base URL must be loopback")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_activation_loopback_url(candidate_url: str, expected_base: str) -> str:
    candidate = _parse_loopback_url(candidate_url)
    expected = _parse_loopback_url(expected_base)

    if (
        candidate.scheme,
        candidate.netloc,
        candidate.path,
    ) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise BrowserLaunchError("loopback URL is not based on configured voice page URL")

    if candidate.fragment:
        raise BrowserLaunchError("loopback URL must not include a fragment")
    if candidate.username is not None or candidate.password is not None:
        raise BrowserLaunchError("loopback URL must not include credentials")
    if not _is_loopback_host(candidate.hostname):
        raise BrowserLaunchError("loopback URL must target a local loopback host")

    query = parse_qs(candidate.query, keep_blank_values=True)
    if set(query.keys()) != {"activation"}:
        raise BrowserLaunchError("loopback URL may only include the activation query parameter")
    if len(query["activation"]) != 1 or not query["activation"][0]:
        raise BrowserLaunchError("loopback URL must include activation token")

    return urlunsplit(
        (
            candidate.scheme,
            candidate.netloc,
            candidate.path,
            urlencode({"activation": query["activation"]}, doseq=True),
            "",
        )
    )


def _parse_loopback_url(url: str):
    parsed = urlsplit(url)
    if not parsed.scheme:
        raise BrowserLaunchError("loopback URL must include a scheme")
    if parsed.scheme not in {"http", "https"}:
        raise BrowserLaunchError("loopback URL must use http or https")
    if not parsed.hostname:
        raise BrowserLaunchError("loopback URL must include a hostname")
    return parsed


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}
