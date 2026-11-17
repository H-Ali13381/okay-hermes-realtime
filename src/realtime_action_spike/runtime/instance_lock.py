from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import suppress
from pathlib import Path
from types import TracebackType


class ControllerInstanceLockError(RuntimeError):
    """The controller singleton lock could not be safely used."""


class ControllerAlreadyRunningError(ControllerInstanceLockError):
    """Another controller already owns the singleton lock."""


def default_controller_lock_path(xdg_runtime_dir: str | None = None) -> Path:
    runtime_root = xdg_runtime_dir or os.getenv("XDG_RUNTIME_DIR")
    if not runtime_root:
        runtime_root = f"/run/user/{os.getuid()}"
    return Path(runtime_root) / "okay-hermes-realtime" / "controller.lock"


class ControllerInstanceLock:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return

        if not self._path.is_absolute():
            raise ControllerInstanceLockError("controller lock path must be absolute")

        parent = self._path.parent
        # The runtime directory is trusted, but we fail closed if any already-existing
        # ancestor is a symlink so mkdir/chmod never operate through a redirected path.
        for candidate in (parent, *parent.parents):
            if candidate.is_symlink():
                raise ControllerInstanceLockError("controller lock directory is unsafe")

        try:
            parent.mkdir(parents=True, exist_ok=True)
            parent.chmod(0o700)
        except OSError as exc:
            raise ControllerInstanceLockError("controller lock directory is unsafe") from exc

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ControllerInstanceLockError("controller lock file is unsafe") from exc

        try:
            try:
                file_status = os.fstat(fd)
                if not stat.S_ISREG(file_status.st_mode):
                    raise ControllerInstanceLockError("controller lock must be a regular file")
                os.fchmod(fd, 0o600)
                os.set_inheritable(fd, False)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ControllerAlreadyRunningError("controller already running") from exc
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        raise ControllerAlreadyRunningError("controller already running") from exc
                    raise ControllerInstanceLockError(
                        "controller lock could not be acquired"
                    ) from exc
            except OSError as exc:
                raise ControllerInstanceLockError(
                    "controller lock could not be safely initialized"
                ) from exc
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            raise

        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return

        self._fd = None
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(fd)

    def __enter__(self) -> ControllerInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
