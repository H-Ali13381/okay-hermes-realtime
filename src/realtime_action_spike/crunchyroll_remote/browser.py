from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import RemotePrototypeError

ProcessFactory = Callable[..., subprocess.Popen]


class BraveRemoteLauncher:
    """Launch one isolated Brave app window with loopback-only CDP."""

    def __init__(
        self,
        *,
        brave_binary: str,
        profile_path: Path,
        debug_port: int,
        process_factory: ProcessFactory = subprocess.Popen,
    ) -> None:
        self._binary = _resolve_executable(brave_binary)
        self._profile = profile_path.expanduser()
        if not 1 <= debug_port <= 65_535:
            raise ValueError("debug port is out of range")
        self._debug_port = debug_port
        self._process_factory = process_factory

    def launch(self, start_url: str) -> subprocess.Popen:
        _validate_start_url(start_url)
        self._profile.mkdir(parents=True, exist_ok=True)
        self._profile.chmod(stat.S_IRWXU)
        args = [
            self._binary,
            f"--user-data-dir={self._profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._debug_port}",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-background-mode",
            f"--app={start_url}",
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
            raise RemotePrototypeError("Brave exited immediately")
        return process


def _validate_start_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/host/")
    ):
        raise RemotePrototypeError("host page must be a secret-free loopback URL")


def _resolve_executable(value: str) -> str:
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        resolved = shutil.which(value)
        if resolved is None:
            raise RemotePrototypeError(f"Brave executable was not found: {value}")
        candidate = Path(resolved)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RemotePrototypeError(f"Brave executable is not executable: {candidate}")
    return str(candidate.resolve())
