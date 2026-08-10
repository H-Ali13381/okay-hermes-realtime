from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from realtime_action_spike.runtime.instance_lock import (
    ControllerAlreadyRunningError,
    ControllerInstanceLock,
    ControllerInstanceLockError,
    default_controller_lock_path,
)


def test_default_controller_lock_path_prefers_explicit_runtime_dir() -> None:
    assert default_controller_lock_path("/tmp/runtime-root") == Path(
        "/tmp/runtime-root/okay-hermes-realtime/controller.lock"
    )


def test_default_controller_lock_path_uses_env_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime")
    assert default_controller_lock_path() == Path(
        "/tmp/xdg-runtime/okay-hermes-realtime/controller.lock"
    )

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert default_controller_lock_path() == Path(
        f"/run/user/{os.getuid()}/okay-hermes-realtime/controller.lock"
    )


def test_first_lock_acquire_release_and_reacquire(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    lock = ControllerInstanceLock(path)

    assert not lock.acquired

    lock.acquire()
    assert lock.acquired
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.get_inheritable(lock._fd) is False  # type: ignore[attr-defined]

    lock.release()
    assert not lock.acquired
    assert path.exists()

    lock.release()
    assert not lock.acquired

    lock.acquire()
    assert lock.acquired
    lock.release()
    assert not lock.acquired


def test_second_lock_fails_while_first_is_held(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    first = ControllerInstanceLock(path)
    second = ControllerInstanceLock(path)

    first.acquire()
    with pytest.raises(ControllerAlreadyRunningError, match="already running"):
        second.acquire()

    first.release()


def test_relative_paths_fail_closed() -> None:
    with pytest.raises(ControllerInstanceLockError, match="absolute"):
        ControllerInstanceLock(Path("runtime/controller.lock")).acquire()


def test_unusable_parent_path_raises_bounded_lock_directory_error(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    path.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        ControllerInstanceLockError,
        match="controller lock directory is unsafe",
    ) as excinfo:
        ControllerInstanceLock(path).acquire()

    assert isinstance(excinfo.value.__cause__, OSError)


def test_symlinked_parent_component_fails_closed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime-target"
    runtime_root.mkdir()
    runtime_root.chmod(0o755)

    runtime_link = tmp_path / "runtime"
    runtime_link.symlink_to(runtime_root, target_is_directory=True)

    path = runtime_link / "controller.lock"
    target_lock = runtime_root / "controller.lock"

    with pytest.raises(ControllerInstanceLockError, match="unsafe"):
        ControllerInstanceLock(path).acquire()

    assert runtime_link.is_symlink()
    assert not target_lock.exists()
    assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    ("failing_attr", "exc_type"),
    [
        ("fstat", OSError),
        ("fchmod", PermissionError),
        ("set_inheritable", OSError),
    ],
)
def test_non_contention_setup_errors_become_instance_lock_errors_and_close_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_attr: str,
    exc_type: type[BaseException],
) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    lock = ControllerInstanceLock(path)
    seen: dict[str, int | None] = {"fd": None}

    original_open = os.open
    original_attr = getattr(os, failing_attr)
    original_fstat = os.fstat

    def wrapped_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        seen["fd"] = fd
        return fd

    def failing_call(*args, **kwargs):
        raise exc_type(f"boom in {failing_attr}")

    monkeypatch.setattr(os, "open", wrapped_open)
    monkeypatch.setattr(os, failing_attr, failing_call)

    with pytest.raises(ControllerInstanceLockError, match=r"controller lock"):
        lock.acquire()

    assert not lock.acquired
    assert seen["fd"] is not None
    with pytest.raises(OSError):
        original_fstat(seen["fd"])

    monkeypatch.setattr(os, failing_attr, original_attr)


def test_holder_subprocess_releases_lock_for_new_owner(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    pythonpath = [str(project_root / "src")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    code = textwrap.dedent(
        """
        from __future__ import annotations

        import sys
        from pathlib import Path

        from realtime_action_spike.runtime.instance_lock import ControllerInstanceLock

        path = Path(sys.argv[1])
        lock = ControllerInstanceLock(path)
        lock.acquire()
        print("ready", flush=True)
        try:
            sys.stdin.read()
        finally:
            lock.release()
        """
    ).strip()

    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        inode_before = path.stat().st_ino

        proc.terminate()
        proc.wait(timeout=5)
        assert proc.returncode is not None

        assert path.exists()
        assert path.stat().st_ino == inode_before

        lock = ControllerInstanceLock(path)
        lock.acquire()
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
