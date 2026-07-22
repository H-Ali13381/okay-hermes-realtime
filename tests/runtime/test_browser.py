from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from realtime_action_spike.runtime.browser import (
    BrowserLaunchError,
    DedicatedBraveLauncher,
    DedicatedBrowserHandle,
)


class FakeProcess:
    """Small synthetic process object used to assert launcher/handle behavior."""

    def __init__(
        self, pid: int, poll_value: int | None = None, wait_results: list[Any] | None = None
    ) -> None:
        self.pid = pid
        self.poll_value = poll_value
        self.wait_results = list(wait_results or [0])
        self.wait_calls: list[float | None] = []
        self.terminate_calls = 0
        self.kwargs: dict[str, object] = {}

    def poll(self) -> int | None:
        return self.poll_value

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        result = self.wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@dataclass
class ProcessFactory:
    process: FakeProcess | None = None
    calls: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: object) -> FakeProcess:
        if self.process is None:
            raise RuntimeError("process not configured")
        # ensure launcher always invokes the process factory with captured kwargs.
        self.calls.append((list(args), dict(kwargs)))
        self.process.kwargs = kwargs
        return self.process


@dataclass
class KillCapture:
    calls: list[tuple[int, int]] = field(default_factory=list)

    def __call__(self, process_group_id: int, sig: int) -> None:
        self.calls.append((process_group_id, sig))


@dataclass
class GroupProbe:
    responses: list[bool]
    calls: list[int] = field(default_factory=list)

    def __call__(self, process_group_id: int) -> bool:
        self.calls.append(process_group_id)
        if self.responses:
            return self.responses.pop(0)
        return False


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_launch_builds_expected_command_and_process_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = make_executable(tmp_path / "brave-bin")
    profile = tmp_path / "dedicated-profile"
    process = FakeProcess(pid=123)
    factory = ProcessFactory(process=process)

    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(profile),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=factory,
    )

    url = launcher._build_loopback_url("secret-token")
    handle = launcher.launch(url)

    assert isinstance(handle, DedicatedBrowserHandle)
    assert factory.calls == [
        (
            [
                str(binary.resolve()),
                f"--user-data-dir={profile}",
                f"--app={url}",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-background-mode",
                "--use-fake-ui-for-media-stream",
            ],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "start_new_session": True,
            },
        )
    ]
    assert handle.process_group_id == process.pid
    assert handle.close_timeout_seconds == 0.6
    assert process.kwargs["stdin"] == subprocess.DEVNULL
    assert process.kwargs["stdout"] == subprocess.DEVNULL
    assert process.kwargs["stderr"] == subprocess.DEVNULL
    assert process.kwargs["shell"] is False
    assert process.kwargs["start_new_session"] is True


def test_launch_restores_vendor_wrapper_environment_for_direct_brave_binary(
    tmp_path: Path,
) -> None:
    binary = make_executable(tmp_path / "brave")
    wrapper = make_executable(tmp_path / "brave-origin")
    process = FakeProcess(pid=123)
    factory = ProcessFactory(process=process)
    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(tmp_path / "profile"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=factory,
    )

    launcher.launch(launcher._build_loopback_url("secret-token"))

    environment = factory.calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert environment["CHROME_WRAPPER"] == str(wrapper.resolve())
    assert environment["CHROME_VERSION_EXTRA"] == "nightly"
    assert environment["GNOME_DISABLE_CRASH_DIALOG"] == "SET_BY_GOOGLE_CHROME"


def test_launch_rejects_non_loopback_voice_url_and_sanitizes_token_in_error(
    tmp_path: Path,
) -> None:
    binary = make_executable(tmp_path / "brave-bin")
    process = FakeProcess(pid=123)
    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(tmp_path / "profile"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=process),
    )

    with pytest.raises(BrowserLaunchError, match="loopback") as exc:
        launcher.launch("http://10.0.0.1:8765/voice?activation=abc123")

    assert "abc123" not in str(exc.value)


def test_launch_rejects_credentials_fragment_and_unexpected_queries(
    tmp_path: Path,
) -> None:
    binary = make_executable(tmp_path / "brave-bin")
    process = FakeProcess(pid=123)
    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(tmp_path / "profile"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=process),
    )

    with pytest.raises(BrowserLaunchError):
        launcher.launch("http://user:pass@127.0.0.1:8765/voice?activation=abc")

    with pytest.raises(BrowserLaunchError):
        launcher.launch("http://127.0.0.1:8765/voice?activation=abc#fragment")

    with pytest.raises(BrowserLaunchError):
        launcher.launch("http://127.0.0.1:8765/voice?activation=abc&unexpected=1")

    with pytest.raises(BrowserLaunchError):
        launcher.launch("http://127.0.0.1:8765/voice")


def test_launch_supports_absolute_binary_and_resolves_bare_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absolute_binary = make_executable(tmp_path / "brave-bin-abs")
    which_binary = make_executable(tmp_path / "brave-bin-which")

    launcher = DedicatedBraveLauncher(
        brave_binary=str(absolute_binary),
        brave_profile=str(tmp_path / "profile"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=FakeProcess(pid=111)),
    )
    assert launcher._resolved_brave_binary == str(absolute_binary.resolve())

    def fake_which(cmd: str) -> str | None:
        if cmd == "brave-origin-nightly":
            return str(which_binary)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    launcher = DedicatedBraveLauncher(
        brave_binary="brave-origin-nightly",
        brave_profile=str(tmp_path / "profile2"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=FakeProcess(pid=112)),
    )
    assert launcher._resolved_brave_binary == str(which_binary)

    with pytest.raises(BrowserLaunchError):
        DedicatedBraveLauncher(
            brave_binary="missing-command",
            brave_profile=str(tmp_path / "profile3"),
            loopback_base_url="http://127.0.0.1:8765/voice",
            process_factory=ProcessFactory(process=FakeProcess(pid=113)),
        )


def test_launch_creates_dedicated_profile_directory_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    binary = make_executable(tmp_path / "brave-bin")
    profile = tmp_path / "dedicated" / "profile"
    process = FakeProcess(pid=555)

    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(profile),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=process),
    )
    launcher.launch(launcher._build_loopback_url("token-1"))

    mode = os.stat(profile).st_mode
    assert profile.exists()
    assert oct(mode & 0o777) == "0o700"


def test_launch_raises_if_process_exits_immediately(tmp_path: Path) -> None:
    binary = make_executable(tmp_path / "brave-bin")
    process = FakeProcess(pid=123, poll_value=1)

    launcher = DedicatedBraveLauncher(
        brave_binary=str(binary),
        brave_profile=str(tmp_path / "profile"),
        loopback_base_url="http://127.0.0.1:8765/voice",
        process_factory=ProcessFactory(process=process),
    )

    with pytest.raises(BrowserLaunchError) as exc:
        launcher.launch(launcher._build_loopback_url("token-early"))
    assert "abc" not in str(exc.value)


def make_handle(
    process: FakeProcess,
    *,
    close_timeout_seconds: float = 2.0,
    kill_capture: KillCapture | None = None,
    group_probe: GroupProbe | None = None,
    clock: FakeClock | None = None,
) -> DedicatedBrowserHandle:
    fake_clock = clock or FakeClock()
    return DedicatedBrowserHandle(
        process=process,
        process_group_id=process.pid,
        close_timeout_seconds=close_timeout_seconds,
        kill_process_group=kill_capture or KillCapture(),
        process_group_exists=group_probe or GroupProbe([False]),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    )


def test_handle_close_is_idempotent_when_process_already_exited() -> None:
    kill = KillCapture()
    process = FakeProcess(pid=42, poll_value=0)
    handle = make_handle(process, kill_capture=kill, close_timeout_seconds=0.01)

    handle.close()
    handle.close()

    assert process.wait_calls == []
    assert kill.calls == []


def test_handle_close_kills_and_waits_for_children_after_main_process_exits() -> None:
    kill = KillCapture()
    probe = GroupProbe([True, True, False])
    clock = FakeClock()
    process = FakeProcess(pid=43, poll_value=0)
    handle = make_handle(
        process,
        kill_capture=kill,
        group_probe=probe,
        clock=clock,
        close_timeout_seconds=0.1,
    )

    handle.close()

    assert kill.calls == [(43, signal.SIGKILL)]
    assert probe.calls == [43, 43, 43]
    assert clock.now > 0


def test_handle_close_waits_for_page_driven_graceful_exit_before_signaling() -> None:
    process = FakeProcess(
        pid=84,
        poll_value=None,
        wait_results=[0],
    )
    kill = KillCapture()
    handle = make_handle(process, close_timeout_seconds=0.01, kill_capture=kill)

    handle.close()

    assert kill.calls == []
    assert process.terminate_calls == 0
    assert process.wait_calls == [0.01]


def test_handle_close_terminates_only_main_pid_after_graceful_exit_timeout() -> None:
    timed_out = subprocess.TimeoutExpired("brave", 0.01)
    process = FakeProcess(pid=84, poll_value=None, wait_results=[timed_out, 0])
    kill = KillCapture()
    handle = make_handle(process, close_timeout_seconds=0.01, kill_capture=kill)

    handle.close()

    assert process.terminate_calls == 1
    assert kill.calls == []
    assert process.wait_calls == [0.01, 0.01]


def test_handle_close_escalates_to_sigkill_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out = subprocess.TimeoutExpired("brave", 0.01)
    process = FakeProcess(
        pid=85,
        poll_value=None,
        wait_results=[timed_out, timed_out, 0],
    )
    kill = KillCapture()
    handle = make_handle(process, close_timeout_seconds=0.01, kill_capture=kill)

    handle.close()

    assert process.terminate_calls == 1
    assert kill.calls == [(85, signal.SIGKILL)]
    assert process.wait_calls == [0.01, 0.01, 0.01]


def test_handle_close_raises_sanitized_error_and_failed_cleanup_remains_retryable() -> None:
    def fail_kill(_process_group_id: int, _sig: int) -> None:
        raise RuntimeError("secret-token-abc")

    process = FakeProcess(
        pid=99,
        poll_value=None,
        wait_results=[
            subprocess.TimeoutExpired("brave-graceful", 0.01),
            subprocess.TimeoutExpired("brave-term", 0.01),
            subprocess.TimeoutExpired("brave-retry-graceful", 0.01),
            subprocess.TimeoutExpired("brave-retry-term", 0.01),
        ],
    )
    handle = DedicatedBrowserHandle(
        process=process,
        process_group_id=process.pid,
        close_timeout_seconds=0.01,
        kill_process_group=fail_kill,
    )

    with pytest.raises(BrowserLaunchError) as exc:
        handle.close()
    assert "secret-token-abc" not in str(exc.value)

    with pytest.raises(BrowserLaunchError):
        handle.close()
    assert process.terminate_calls == 2
    assert str(exc.value) != ""


def test_handle_close_fallback_only_targets_owned_process_group() -> None:
    process = FakeProcess(
        pid=1234,
        poll_value=None,
        wait_results=[
            subprocess.TimeoutExpired("brave-graceful", 0.01),
            subprocess.TimeoutExpired("brave-term", 0.01),
            0,
        ],
    )
    kill = KillCapture()
    handle = make_handle(process, close_timeout_seconds=0.01, kill_capture=kill)

    handle.close()

    assert process.terminate_calls == 1
    assert kill.calls == [(1234, signal.SIGKILL)]
    assert all(group == process.pid for group, _ in kill.calls)
    assert all(sig == signal.SIGKILL for _, sig in kill.calls)
