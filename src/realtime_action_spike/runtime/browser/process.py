from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable

from .contracts import BrowserLaunchError

KillProcessGroup = Callable[[int, int], None]
ProcessGroupExists = Callable[[int], bool]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


class DedicatedBrowserHandle:
    """Own one browser process and tear it down via its process group."""

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

            try:
                self._process.wait(timeout=self._phase_timeout(deadline))
                self._ensure_owned_process_group_exited(deadline)
                self._closed = True
                return
            except subprocess.TimeoutExpired:
                pass

            # TERM only the browser main PID. Signalling every Chromium child at
            # once causes Brave/Crashpad to report a SIGTRAP crash.
            self._process.terminate()
            try:
                self._process.wait(timeout=self._phase_timeout(deadline))
            except subprocess.TimeoutExpired:
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


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True
