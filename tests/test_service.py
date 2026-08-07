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

    async def close_active_session(self) -> None:
        self.close_calls += 1

    async def shutdown_task_events(self) -> None:
        self.task_event_shutdown_calls += 1


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


@pytest.mark.asyncio
async def test_shutdown_closes_active_session_before_http_and_activation_socket() -> None:
    harness = _harness()
    events: list[str] = []
    original_controller_close = harness.controller.close_active_session
    original_http_shutdown = harness.http.shutdown
    original_socket_stop = harness.socket.stop

    async def close_active_session() -> None:
        events.append("controller_close")
        await original_controller_close()

    async def shutdown_http() -> None:
        events.append("http_shutdown")
        await original_http_shutdown()

    async def stop_socket() -> None:
        events.append("socket_stop")
        await original_socket_stop()

    harness.controller.close_active_session = close_active_session
    harness.http.shutdown = shutdown_http
    harness.socket.stop = stop_socket

    run_task = asyncio.create_task(harness.service.run())
    await asyncio.wait_for(harness.http.serve_entered.wait(), timeout=1.0)
    harness.http.started.set()
    await asyncio.wait_for(harness.service.ready.wait(), timeout=1.0)

    harness.service.request_shutdown()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert events == ["controller_close", "http_shutdown", "socket_stop"]


@pytest.mark.asyncio
async def test_controller_health_marker_tracks_service_readiness_and_shutdown(
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
    assert not marker.exists()


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


def test_service_module_executes_main_only_under_module_entrypoint() -> None:
    source = Path(service_module.__file__).read_text(encoding="utf-8")

    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
