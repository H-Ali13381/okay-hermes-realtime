from __future__ import annotations

import asyncio
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

    async def close_active_session(self) -> None:
        self.close_calls += 1


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


def test_service_module_executes_main_only_under_module_entrypoint() -> None:
    source = Path(service_module.__file__).read_text(encoding="utf-8")

    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
