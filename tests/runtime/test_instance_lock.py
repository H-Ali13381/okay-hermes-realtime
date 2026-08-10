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


def test_symlink_final_path_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    target = tmp_path / "escaped" / "controller.lock"
    target.parent.mkdir(parents=True)
    target.write_text("do not follow", encoding="utf-8")
    path.parent.mkdir(parents=True)
    path.symlink_to(target)

    with pytest.raises(ControllerInstanceLockError, match=r"unsafe|regular file"):
        ControllerInstanceLock(path).acquire()

    assert path.is_symlink()
    assert target.read_text(encoding="utf-8") == "do not follow"


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
