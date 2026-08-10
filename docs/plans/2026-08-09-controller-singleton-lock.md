# Controller Singleton Lock Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Prevent every second production controller process from reaching `RuntimeService` initialization by acquiring one crash-safe per-user `flock` at the installed controller entrypoint.

**Architecture:** Add a focused `ControllerInstanceLock` under the runtime package. The production `service.main()` resolves and acquires it before loading settings or constructing `RuntimeService`, holds its non-inheritable descriptor for the process lifetime, and returns a bounded nonzero result on contention. Existing activation-socket and HTTP-port checks remain defense in depth.

**Tech Stack:** Python 3.12, `fcntl.flock`, `os.open`, pathlib, pytest, pytest monkeypatch/subprocess fixtures, systemd user services.

**Design:** `docs/superpowers/specs/2026-08-09-controller-singleton-lock-design.md`

---

### Task 1: Add the kernel-backed singleton lock

**Objective:** Build and test a small lock component that provides atomic per-user controller ownership with crash-safe release.

**Files:**
- Create: `src/realtime_action_spike/runtime/instance_lock.py`
- Create: `tests/runtime/test_instance_lock.py`

**Step 1: Write failing lock tests**

Add tests covering:

```python
def test_second_lock_fails_while_first_is_held(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.lock"
    first = ControllerInstanceLock(path)
    second = ControllerInstanceLock(path)

    first.acquire()
    with pytest.raises(ControllerAlreadyRunningError):
        second.acquire()

    first.release()
```

Also add tests that:

- acquisition succeeds after `release()`;
- repeated `release()` is safe;
- relative paths fail with `ControllerInstanceLockError`;
- a symlink at the final lock path fails closed;
- the acquired descriptor is non-inheritable;
- the private parent directory and lock file are modes `0700` and `0600`;
- a holder subprocess can exit without deleting the inode and a new process can immediately acquire it.

The subprocess test must import the real implementation, signal readiness only after acquisition, then block on stdin. The parent closes/terminates the holder, verifies its exit, and acquires the same path.

**Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest -q tests/runtime/test_instance_lock.py
```

Expected: collection failure because `realtime_action_spike.runtime.instance_lock` does not exist.

**Step 3: Implement the minimal lock**

Create `instance_lock.py` with:

```python
from __future__ import annotations

import fcntl
import os
import stat
from contextlib import suppress
from pathlib import Path
from types import TracebackType


class ControllerInstanceLockError(RuntimeError):
    """The controller singleton lock could not be safely used."""


class ControllerAlreadyRunningError(ControllerInstanceLockError):
    """Another controller owns the per-user runtime."""


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
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent.chmod(0o700)

        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ControllerInstanceLockError("controller lock file is unsafe") from exc

        try:
            file_status = os.fstat(fd)
            if not stat.S_ISREG(file_status.st_mode):
                raise ControllerInstanceLockError("controller lock must be a regular file")
            os.fchmod(fd, 0o600)
            os.set_inheritable(fd, False)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ControllerAlreadyRunningError("controller already running") from exc
        except BaseException:
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
```

Implementation constraints:

- Do not unlink the lock file.
- Do not store or trust a PID.
- Do not signal another process.
- Do not invoke a shell.
- Keep the descriptor non-inheritable.
- Preserve the specific contention exception rather than collapsing it into a generic path failure.

**Step 4: Run focused tests to verify GREEN**

Run:

```bash
uv run ruff check src/realtime_action_spike/runtime/instance_lock.py tests/runtime/test_instance_lock.py
uv run pytest -q tests/runtime/test_instance_lock.py
```

Expected: all lock tests pass and Ruff reports no findings.

**Step 5: Commit the lock component**

```bash
git add src/realtime_action_spike/runtime/instance_lock.py tests/runtime/test_instance_lock.py
git commit -m "feat: add controller singleton lock"
```

---

### Task 2: Enforce the lock at the production entrypoint

**Objective:** Reject duplicate controller invocations before settings loading or `RuntimeService` construction.

**Files:**
- Modify: `src/realtime_action_spike/service.py:1-20,320-330`
- Modify: `tests/test_service.py`

**Step 1: Write failing entrypoint tests**

Add synchronous tests that monkeypatch module collaborators.

Contention test:

```python
def test_main_rejects_duplicate_before_runtime_initialization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BusyLock:
        def __init__(self, _path: Path) -> None:
            pass

        def acquire(self) -> None:
            raise ControllerAlreadyRunningError("controller already running")

        def release(self) -> None:
            raise AssertionError("unreachable")

    monkeypatch.setattr(service_module, "ControllerInstanceLock", BusyLock)
    monkeypatch.setattr(
        service_module.Settings,
        "from_env",
        lambda: pytest.fail("settings must not load after lock contention"),
    )
    monkeypatch.setattr(
        service_module,
        "RuntimeService",
        lambda _settings: pytest.fail("runtime must not initialize"),
    )

    assert service_module.main() == CONTROLLER_ALREADY_RUNNING_EXIT_CODE
    assert capsys.readouterr().err == (
        "Okay Hermes Realtime controller is already running.\n"
    )
```

Also add a successful-path test using a recording context-manager lock and a fake service. Verify ordering:

```text
lock enter → settings load → RuntimeService construct → run → lock exit
```

Add a generic unsafe-lock failure test proving bounded stderr and exit code `1`, again without settings/runtime initialization.

**Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest -q tests/test_service.py -k 'main_rejects_duplicate or main_holds_singleton or main_reports_lock_failure'
```

Expected: failures because `main()` does not acquire the singleton lock or expose the bounded contention result.

**Step 3: Implement entrypoint ownership**

Modify `service.py` to import `sys` and:

```python
from .runtime.instance_lock import (
    ControllerAlreadyRunningError,
    ControllerInstanceLock,
    ControllerInstanceLockError,
    default_controller_lock_path,
)

CONTROLLER_ALREADY_RUNNING_EXIT_CODE = 73
```

Implement this ordering:

```python
def main() -> int:
    lock = ControllerInstanceLock(default_controller_lock_path())
    try:
        lock.acquire()
    except ControllerAlreadyRunningError:
        print("Okay Hermes Realtime controller is already running.", file=sys.stderr)
        return CONTROLLER_ALREADY_RUNNING_EXIT_CODE
    except ControllerInstanceLockError:
        print("Okay Hermes Realtime controller lock could not be acquired.", file=sys.stderr)
        return 1

    try:
        service = RuntimeService(Settings.from_env())
        try:
            asyncio.run(service.run())
            return 0
        except KeyboardInterrupt:
            return 130
    finally:
        lock.release()
```

Do not catch ordinary `RuntimeService` failures as lock failures. Lock-error handling ends before settings/runtime initialization. The outer `finally` must release ownership during every runtime exit while preserving existing runtime exception behavior.

**Step 4: Run focused tests to verify GREEN**

Run:

```bash
uv run ruff check src/realtime_action_spike/service.py tests/test_service.py
uv run pytest -q tests/test_service.py tests/runtime/test_instance_lock.py
```

Expected: all service and lock tests pass.

**Step 5: Commit entrypoint enforcement**

```bash
git add src/realtime_action_spike/service.py tests/test_service.py
git commit -m "fix: reject duplicate controller processes early"
```

---

### Task 3: Verify packaging, service behavior, and live ownership

**Objective:** Prove the singleton survives repository gates and prevents a real second installed controller without disturbing the primary.

**Files:**
- Modify only if verification exposes a defect.

**Step 1: Run repository gates**

```bash
uv run ruff check .
uv run python -m compileall -q src scripts
uv run pytest -q
npm run test:web
npm run check:web
bash -n scripts/install_user_services.sh
bash scripts/build-package.sh
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
git diff --check
```

Expected: all commands exit `0`; Python and web test counts contain no failures.

**Step 2: Install the verified wheel**

```bash
uv pip install \
  --python ~/.local/share/okay-hermes-realtime/venv/bin/python \
  --reinstall \
  dist/openai_realtime_action_spike-0.1.0-py3-none-any.whl
```

Expected: the deployed package is replaced by the wheel from this worktree.

**Step 3: Restart the controlled services**

```bash
systemctl --user restart okay-hermes-realtime-controller.service
systemctl --user restart okay-hermes-realtime-wakeword.service
```

Expected: both units converge to `active/running` with no restart loop.

**Step 4: Exercise the real duplicate path**

Record the primary PID and health-marker hashes/content. Run the installed controller executable directly with a bounded timeout:

```bash
~/.local/share/okay-hermes-realtime/venv/bin/okay-hermes-realtime-controller
```

Expected:

- exit code `73`;
- stderr exactly `Okay Hermes Realtime controller is already running.`;
- command exits immediately;
- primary controller PID is unchanged;
- primary `NRestarts` remains `0`;
- `controller-health` remains `ready`;
- `capture-health` remains `healthy`;
- `/health` remains `status=ok`;
- exactly one controller process remains.

**Step 5: Verify crash-safe release in an isolated runtime**

Use a temporary `XDG_RUNTIME_DIR`, acquire the real lock in a holder subprocess, kill the holder, and verify a new invocation of the lock test helper acquires immediately without deleting the lock inode.

Expected: acquisition succeeds after holder death.

**Step 6: Verify tray convergence**

```bash
systemctl --user show okay-hermes-realtime-tray.service -p MainPID -p NRestarts
busctl --user get-property \
  <tray-bus-name> /StatusNotifierItem org.kde.StatusNotifierItem ToolTip
```

Expected: one current tray process and tooltip `Okay Hermes Realtime: ON`.

**Step 7: Final status and history check**

```bash
git status --short --branch
git log -5 --oneline
```

Expected: clean worktree; singleton design and implementation commits are present.
