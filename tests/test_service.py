from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI

import realtime_action_spike.service as service_module
from realtime_action_spike.config import Settings
from realtime_action_spike.service import RuntimeService, _UvicornServer


class FakeLauncher:
    def launch(self, _url: str) -> object:
        raise AssertionError("browser must not launch during service startup")


class FakeController:
    def __init__(self) -> None:
        self.close_calls = 0
        self.task_event_shutdown_calls = 0
        self.media_shutdown_calls = 0
        self.crunchyroll_shutdown_calls = 0

    async def close_active_session(self) -> None:
        self.close_calls += 1

    async def shutdown_task_events(self) -> None:
        self.task_event_shutdown_calls += 1

    async def shutdown_media(self) -> None:
        self.media_shutdown_calls += 1

    async def shutdown_crunchyroll(self) -> None:
        self.crunchyroll_shutdown_calls += 1


class FakeSocket:
    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stop_calls += 1


class FakeHTTPServer:
    def __init__(
        self,
        *,
        serve_error: Exception | None = None,
        shutdown_releases: bool = True,
    ) -> None:
        self.started = asyncio.Event()
        self.serve_entered = asyncio.Event()
        self.released = asyncio.Event()
        self.serve_error = serve_error
        self.shutdown_releases = shutdown_releases
        self.shutdown_calls = 0
        self.cancelled = False

    async def serve(self) -> None:
        self.serve_entered.set()
        if self.serve_error is not None:
            raise self.serve_error
        try:
            await self.released.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_releases:
            self.released.set()


@dataclass
class ServiceHarness:
    service: RuntimeService
    socket: FakeSocket
    http: FakeHTTPServer
    controller: FakeController


@pytest.fixture(autouse=True)
def isolated_service_state_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Keep service health markers out of the user's live state directory."""
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return state_home


def _harness(
    *,
    socket: FakeSocket | None = None,
    http: FakeHTTPServer | None = None,
    startup_timeout: float = 0.2,
    shutdown_timeout: float = 0.05,
) -> ServiceHarness:
    fake_socket = socket or FakeSocket()
    fake_http = http or FakeHTTPServer()
    controller = FakeController()
    service = RuntimeService(
        Settings(openai_api_key=None),
        launcher_factory=lambda _settings: FakeLauncher(),  # type: ignore[arg-type]
        controller_factory=lambda _launcher: controller,  # type: ignore[arg-type]
        socket_factory=lambda _controller, _settings: fake_socket,  # type: ignore[arg-type]
        app_factory=lambda _settings, _controller: object(),  # type: ignore[arg-type]
        http_server_factory=lambda _app, _settings: fake_http,
        startup_timeout_seconds=startup_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
    )
    return ServiceHarness(service, fake_socket, fake_http, controller)


@pytest.mark.asyncio
async def test_readiness_waits_for_socket_and_http_then_shutdown_is_coordinated() -> None:
    harness = _harness()
    run_task = asyncio.create_task(harness.service.run())

    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    assert harness.socket.start_calls == 1
    assert not harness.service.ready.is_set()

    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)
    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert harness.http.shutdown_calls == 1
    assert harness.socket.stop_calls == 1
    assert harness.controller.close_calls == 1
    assert harness.controller.task_event_shutdown_calls == 1
    assert harness.controller.media_shutdown_calls == 1
    assert harness.controller.crunchyroll_shutdown_calls == 1


@pytest.mark.asyncio
async def test_shutdown_closes_active_session_before_http_and_activation_socket() -> None:
    harness = _harness()
    events: list[str] = []
    original_controller_close = harness.controller.close_active_session
    original_crunchyroll_shutdown = harness.controller.shutdown_crunchyroll
    original_media_shutdown = harness.controller.shutdown_media
    original_http_shutdown = harness.http.shutdown
    original_socket_stop = harness.socket.stop

    async def close_active_session() -> None:
        events.append("controller_close")
        await original_controller_close()

    async def shutdown_crunchyroll() -> None:
        events.append("crunchyroll_shutdown")
        await original_crunchyroll_shutdown()

    async def shutdown_media() -> None:
        events.append("media_shutdown")
        await original_media_shutdown()

    async def shutdown_http() -> None:
        events.append("http_shutdown")
        await original_http_shutdown()

    async def stop_socket() -> None:
        events.append("socket_stop")
        await original_socket_stop()

    harness.controller.close_active_session = close_active_session
    harness.controller.shutdown_crunchyroll = shutdown_crunchyroll
    harness.controller.shutdown_media = shutdown_media
    harness.http.shutdown = shutdown_http
    harness.socket.stop = stop_socket

    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert events == [
        "controller_close",
        "crunchyroll_shutdown",
        "media_shutdown",
        "http_shutdown",
        "socket_stop",
    ]


@pytest.mark.asyncio
async def test_media_cleanup_failure_is_reported_after_other_shutdown_steps_complete(
    isolated_service_state_home: Path,
) -> None:
    harness = _harness()
    marker = isolated_service_state_home / "okay-hermes-realtime" / "controller-health"

    async def fail_media_shutdown() -> None:
        harness.controller.media_shutdown_calls += 1
        raise RuntimeError("mpv did not exit after forced termination")

    harness.controller.shutdown_media = fail_media_shutdown
    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    harness.service.request_shutdown()
    with pytest.raises(RuntimeError, match="media cleanup failed during service shutdown"):
        await asyncio.wait_for(run_task, timeout=1.0)

    assert harness.controller.media_shutdown_calls == 1
    assert harness.http.shutdown_calls == 1
    assert harness.socket.stop_calls == 1
    assert marker.read_text(encoding="utf-8") == "ready\n"


@pytest.mark.asyncio
async def test_controller_health_marker_tracks_service_readiness_and_persists_on_shutdown(
    isolated_service_state_home: Path,
) -> None:
    harness = _harness()
    marker = isolated_service_state_home / "okay-hermes-realtime" / "controller-health"
    run_task = asyncio.create_task(harness.service.run())

    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    assert not marker.exists()

    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)
    assert marker.read_text(encoding="utf-8") == "ready\n"

    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=1.0)
    assert marker.read_text(encoding="utf-8") == "ready\n"


@pytest.mark.asyncio
async def test_failed_secondary_service_does_not_remove_live_health_marker(
    isolated_service_state_home: Path,
) -> None:
    primary = _harness()
    marker = isolated_service_state_home / "okay-hermes-realtime" / "controller-health"
    primary_task = asyncio.create_task(primary.service.run())
    await asyncio.wait_for(primary.http.serve_entered.wait(), timeout=1.0)
    primary.http.started.set()
    await asyncio.wait_for(primary.service.ready.wait(), timeout=1.0)
    assert marker.read_text(encoding="utf-8") == "ready\n"

    secondary = _harness(socket=FakeSocket(start_error=RuntimeError("socket unavailable")))
    with pytest.raises(RuntimeError, match="socket unavailable"):
        await secondary.service.run()

    assert marker.read_text(encoding="utf-8") == "ready\n"

    primary.service.request_shutdown()
    await asyncio.wait_for(primary_task, timeout=1.0)


@pytest.mark.asyncio
async def test_http_startup_failure_is_propagated_and_socket_is_cleaned() -> None:
    failure = RuntimeError("http failed during startup")
    harness = _harness(http=FakeHTTPServer(serve_error=failure))

    with pytest.raises(RuntimeError, match="http failed during startup"):
        await harness.service.run()

    assert harness.socket.start_calls == 1
    assert harness.socket.stop_calls >= 1
    assert harness.controller.close_calls >= 1


@pytest.mark.asyncio
async def test_socket_startup_failure_never_starts_http() -> None:
    failure = RuntimeError("socket unavailable")
    harness = _harness(socket=FakeSocket(start_error=failure))

    with pytest.raises(RuntimeError, match="socket unavailable"):
        await harness.service.run()

    assert not harness.http.serve_entered.is_set()
    assert harness.socket.stop_calls >= 1
    assert harness.controller.close_calls >= 1


@pytest.mark.asyncio
async def test_hung_http_shutdown_is_bounded_and_task_is_cancelled() -> None:
    harness = _harness(
        http=FakeHTTPServer(shutdown_releases=False),
        shutdown_timeout=0.02,
    )
    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert harness.http.cancelled
    assert harness.socket.stop_calls == 1
    assert harness.controller.close_calls == 1


@pytest.mark.asyncio
async def test_shutdown_uses_one_shared_deadline_across_all_phases() -> None:
    harness = _harness(
        http=FakeHTTPServer(shutdown_releases=False),
        shutdown_timeout=0.03,
    )
    never = asyncio.Event()

    async def hang_controller_close() -> None:
        harness.controller.close_calls += 1
        await never.wait()

    async def hang_http_shutdown() -> None:
        harness.http.shutdown_calls += 1
        await never.wait()

    async def hang_socket_stop() -> None:
        harness.socket.stop_calls += 1
        await never.wait()

    harness.controller.close_active_session = hang_controller_close
    harness.http.shutdown = hang_http_shutdown
    harness.socket.stop = hang_socket_stop

    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    started = asyncio.get_running_loop().time()
    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.08
    assert harness.controller.close_calls == 1
    assert harness.http.shutdown_calls == 1
    assert harness.socket.stop_calls == 1


@pytest.mark.asyncio
async def test_service_task_cancellation_still_cleans_all_owned_components() -> None:
    harness = _harness()
    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert harness.http.shutdown_calls == 1
    assert harness.socket.stop_calls == 1
    assert harness.controller.close_calls == 1


@pytest.mark.asyncio
async def test_uvicorn_adapter_publishes_event_readiness_and_stops() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = FastAPI()
    server = _UvicornServer(
        app,
        Settings(gateway_host="127.0.0.1", gateway_port=port),
    )
    serve_task = asyncio.create_task(server.serve())

    await asyncio.wait_for(server.started.wait(), timeout=2.0)
    assert server.started.is_set()
    await server.shutdown()
    await asyncio.wait_for(serve_task, timeout=2.0)


@pytest.mark.asyncio
async def test_uvicorn_adapter_leaves_signal_ownership_to_runtime_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_signals = False

    @contextlib.contextmanager
    def observe_signal_capture(_server: object):
        nonlocal captured_signals
        captured_signals = True
        yield

    monkeypatch.setattr(service_module.uvicorn.Server, "capture_signals", observe_signal_capture)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = _UvicornServer(
        FastAPI(),
        Settings(gateway_host="127.0.0.1", gateway_port=port),
    )
    serve_task = asyncio.create_task(server.serve())

    await asyncio.wait_for(server.started.wait(), timeout=2.0)
    await server.shutdown()
    await asyncio.wait_for(serve_task, timeout=2.0)

    assert not captured_signals


class _RecordingRuntimeService:
    def __init__(self, settings: object, events: list[str]) -> None:
        self.settings = settings
        self._events = events
        self._events.append("runtime construct")

    async def run(self) -> None:
        self._events.append("run")


class _KeyboardInterruptAcquireLock:
    def __init__(self, path: object, events: list[str]) -> None:
        self.path = path
        self._events = events
        self.release_called = False
        self._events.append("lock construct")

    def acquire(self) -> None:
        self._events.append("acquire")
        raise KeyboardInterrupt

    def release(self) -> None:
        self.release_called = True
        self._events.append("release")


class _RecordingLock:
    def __init__(self, path: object, events: list[str]) -> None:
        self.path = path
        self._events = events
        self._events.append("lock construct")

    def acquire(self) -> None:
        self._events.append("acquire")

    def release(self) -> None:
        self._events.append("release")


class _AlreadyRunningLock:
    def __init__(self, _path: object) -> None:
        pass

    def acquire(self) -> None:
        raise service_module.ControllerAlreadyRunningError("controller already running")

    def release(self) -> None:
        raise AssertionError("release must not be called when acquire fails")


class _GenericLockErrorLock:
    def __init__(self, _path: object) -> None:
        pass

    def acquire(self) -> None:
        raise service_module.ControllerInstanceLockError("controller lock could not be acquired")

    def release(self) -> None:
        raise AssertionError("release must not be called when acquire fails")


def test_main_rejects_duplicate_controller_processes_early(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = {"settings": False, "runtime": False}

    monkeypatch.setattr(service_module, "ControllerInstanceLock", _AlreadyRunningLock)
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(
        service_module.Settings, "from_env", lambda: called.__setitem__("settings", True) or None
    )
    monkeypatch.setattr(
        service_module,
        "RuntimeService",
        lambda _settings: called.__setitem__("runtime", True) or None,
    )

    exit_code = service_module.main()
    captured = capsys.readouterr()

    assert exit_code == service_module.CONTROLLER_ALREADY_RUNNING_EXIT_CODE
    assert captured.err == "Okay Hermes Realtime controller is already running.\n"
    assert called == {"settings": False, "runtime": False}


def test_main_reports_generic_controller_lock_failures_without_touching_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = {"settings": False, "runtime": False}

    monkeypatch.setattr(service_module, "ControllerInstanceLock", _GenericLockErrorLock)
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(
        service_module.Settings, "from_env", lambda: called.__setitem__("settings", True) or None
    )
    monkeypatch.setattr(
        service_module,
        "RuntimeService",
        lambda _settings: called.__setitem__("runtime", True) or None,
    )

    exit_code = service_module.main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err == "Okay Hermes Realtime controller lock could not be acquired.\n"
    assert called == {"settings": False, "runtime": False}


def test_service_wires_configured_youtube_resolver_into_controller() -> None:
    service = RuntimeService(
        Settings(openai_api_key=None, yt_dlp_bin="/opt/bin/yt-dlp"),
        launcher_factory=lambda _settings: FakeLauncher(),  # type: ignore[arg-type]
        socket_factory=lambda _controller, _settings: FakeSocket(),  # type: ignore[arg-type]
        app_factory=lambda _settings, _controller: object(),  # type: ignore[arg-type]
        http_server_factory=lambda _app, _settings: FakeHTTPServer(),
    )

    assert service._controller._youtube_resolver.yt_dlp_bin == "/opt/bin/yt-dlp"


def test_main_returns_130_when_lock_acquire_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    called = {"settings": False, "runtime": False}
    fake_lock = _KeyboardInterruptAcquireLock(Path("/tmp/controller.lock"), events)

    def fake_from_env() -> object:
        called["settings"] = True
        raise AssertionError("settings must not be loaded when lock acquisition is interrupted")

    def fake_runtime_service(_settings: object) -> object:
        called["runtime"] = True
        raise AssertionError(
            "runtime service must not be constructed when lock acquisition is interrupted"
        )

    monkeypatch.setattr(service_module, "ControllerInstanceLock", lambda _path: fake_lock)
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", fake_runtime_service)

    exit_code = service_module.main()

    assert exit_code == 130
    assert events == ["lock construct", "acquire"]
    assert fake_lock.release_called is False
    assert called == {"settings": False, "runtime": False}


def test_main_loads_settings_after_lock_before_constructing_runtime_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        return object()

    def fake_runtime_service(settings: object) -> _RecordingRuntimeService:
        return _RecordingRuntimeService(settings, events)

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", fake_runtime_service)

    exit_code = service_module.main()

    assert exit_code == 0
    assert events == [
        "lock construct",
        "acquire",
        "settings",
        "runtime construct",
        "run",
        "release",
    ]


def test_main_propagates_settings_failure_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        raise RuntimeError("settings failed")

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    runtime_called = False

    def fake_runtime_service(_settings: object) -> object:
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("runtime service must not be constructed when settings fail")

    monkeypatch.setattr(service_module, "RuntimeService", fake_runtime_service)

    with pytest.raises(RuntimeError, match="settings failed"):
        service_module.main()

    assert runtime_called is False
    assert events == ["lock construct", "acquire", "settings", "release"]


@pytest.mark.parametrize(
    ("interrupt_point", "expected_events"),
    [
        (
            "settings",
            ["lock construct", "acquire", "settings", "release"],
        ),
        (
            "runtime",
            ["lock construct", "acquire", "settings", "runtime construct", "release"],
        ),
    ],
)
def test_main_returns_130_for_keyboard_interrupt_during_startup_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_point: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        if interrupt_point == "settings":
            raise KeyboardInterrupt
        return object()

    def fake_runtime_service(settings: object) -> object:
        events.append("runtime construct")
        if interrupt_point == "runtime":
            raise KeyboardInterrupt
        return _RecordingRuntimeService(settings, events)

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", fake_runtime_service)

    exit_code = service_module.main()

    assert exit_code == 130
    assert events == expected_events


def test_main_releases_lock_when_runtime_service_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        return object()

    def fake_runtime_service(_settings: object) -> object:
        events.append("runtime construct")
        raise RuntimeError("runtime constructor failed")

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", fake_runtime_service)

    with pytest.raises(RuntimeError, match="runtime constructor failed"):
        service_module.main()

    assert events == ["lock construct", "acquire", "settings", "runtime construct", "release"]


def test_main_releases_lock_when_runtime_run_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        return object()

    class FakeRuntimeService:
        def __init__(self, _settings: object) -> None:
            events.append("runtime construct")

        async def run(self) -> None:
            events.append("run")
            raise RuntimeError("runtime failed")

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", FakeRuntimeService)

    with pytest.raises(RuntimeError, match="runtime failed"):
        service_module.main()

    assert events == [
        "lock construct",
        "acquire",
        "settings",
        "runtime construct",
        "run",
        "release",
    ]


def test_main_returns_130_for_keyboard_interrupt_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fake_from_env() -> object:
        events.append("settings")
        return object()

    class FakeRuntimeService:
        def __init__(self, _settings: object) -> None:
            events.append("runtime construct")

        async def run(self) -> None:
            events.append("run")
            raise KeyboardInterrupt

    monkeypatch.setattr(
        service_module, "ControllerInstanceLock", lambda path: _RecordingLock(path, events)
    )
    monkeypatch.setattr(
        service_module, "default_controller_lock_path", lambda: Path("/tmp/controller.lock")
    )
    monkeypatch.setattr(service_module.Settings, "from_env", fake_from_env)
    monkeypatch.setattr(service_module, "RuntimeService", FakeRuntimeService)

    exit_code = service_module.main()

    assert exit_code == 130
    assert events == [
        "lock construct",
        "acquire",
        "settings",
        "runtime construct",
        "run",
        "release",
    ]


def test_service_module_executes_main_only_under_module_entrypoint() -> None:
    source = Path(service_module.__file__).read_text(encoding="utf-8")

    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
