from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Protocol

import uvicorn

from .config import Settings
from .gateway import create_app
from .runtime.activation_socket import ActivationSocket
from .runtime.browser import DedicatedBraveLauncher
from .runtime.controller import VoiceSessionController


class HTTPServerProtocol(Protocol):
    started: asyncio.Event

    async def serve(self) -> None: ...
    async def shutdown(self) -> None: ...


class _EmbeddedUvicornServer(uvicorn.Server):
    """Run under RuntimeService's coordinated signal ownership."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class _UvicornServer:
    def __init__(self, app: object, settings: Settings) -> None:
        self._server = _EmbeddedUvicornServer(
            uvicorn.Config(
                app,
                host=settings.gateway_host,
                port=settings.gateway_port,
                access_log=False,
                loop="asyncio",
            )
        )
        self.started = asyncio.Event()

    async def serve(self) -> None:
        serve_task = asyncio.create_task(self._server.serve())
        try:
            while not self._server.started:
                if serve_task.done():
                    exception = serve_task.exception()
                    if exception is not None:
                        raise exception
                    raise RuntimeError("uvicorn stopped before readiness")
                await asyncio.sleep(0.01)
            self.started.set()
            await serve_task
        finally:
            if not serve_task.done():
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task

    async def shutdown(self) -> None:
        self._server.should_exit = True
        await asyncio.sleep(0)


class RuntimeService:
    """Coordinated runtime lifecycle for socket, HTTP app, and browser controller."""

    def __init__(
        self,
        settings: Settings,
        *,
        launcher_factory: Callable[[Settings], DedicatedBraveLauncher] = (
            lambda settings: DedicatedBraveLauncher(
                brave_binary=settings.brave_bin,
                brave_profile=settings.voice_browser_profile,
                loopback_base_url=settings.voice_page_url,
                start_timeout_seconds=settings.voice_browser_start_timeout_seconds,
            )
        ),
        controller_factory: (
            Callable[[DedicatedBraveLauncher], VoiceSessionController] | None
        ) = None,
        socket_factory: Callable[[VoiceSessionController, Settings], ActivationSocket] = (
            lambda controller, settings: ActivationSocket(
                controller=controller,
                voice_page_url=settings.voice_page_url,
                path=settings.resolved_activation_socket_path,
            )
        ),
        app_factory: Callable[[Settings, VoiceSessionController], object] = (
            lambda settings, controller: create_app(settings, controller=controller)
        ),
        http_server_factory: Callable[[object, Settings], HTTPServerProtocol] = (
            lambda app, settings: _UvicornServer(app, settings)
        ),
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.launcher_factory = launcher_factory
        self.controller_factory = controller_factory
        self.socket_factory = socket_factory
        self.app_factory = app_factory
        self.http_server_factory = http_server_factory
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds

        self._ready = asyncio.Event()
        self._shutdown_requested = asyncio.Event()
        self._running = False
        self._started = False
        self._shutdown_complete = False
        self._signal_handlers_registered = False

        self._launcher = launcher_factory(settings)
        state_home = Path(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
        )
        self._controller_health_path = (
            state_home / "okay-hermes-realtime" / "controller-health"
        )
        if controller_factory is None:
            self._controller = VoiceSessionController(
                launcher=self._launcher,
                trace_directory=state_home / "okay-hermes-realtime" / "traces",
                status_observer=self._publish_controller_status,
            )
        else:
            self._controller = controller_factory(self._launcher)
        self._socket = socket_factory(self._controller, settings)
        self._app = app_factory(settings, self._controller)
        self._http_server = http_server_factory(self._app, settings)
        self._http_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> asyncio.Event:
        return self._ready

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("service already running")

        self._running = True
        await self._install_signal_handlers()

        try:
            await self._startup()
            self._ready.set()
            await self._shutdown_requested.wait()
        finally:
            await self._shutdown()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()

    async def _startup(self) -> None:
        if self._started:
            return

        try:
            await self._socket.start()
            self._http_task = asyncio.create_task(self._http_server.serve())
            await asyncio.wait_for(
                self._await_http_ready(),
                timeout=self.startup_timeout_seconds,
            )
            self._started = True
            self._publish_controller_status(getattr(self._controller, "status", "idle"))
        except Exception:
            await self._shutdown()
            raise

    async def _await_http_ready(self) -> None:
        while True:
            if self._http_task is None:
                await asyncio.sleep(0)
                continue

            if self._http_task.done():
                if self._http_task.cancelled():
                    raise RuntimeError("http server cancelled during startup")
                exception = self._http_task.exception()
                if exception is not None:
                    raise exception
                raise RuntimeError("http server stopped during startup")

            if self._http_server.started.is_set():
                return

            await asyncio.sleep(0.01)

    async def _shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_timeout_seconds

        def remaining_timeout() -> float:
            # A tiny floor lets every cleanup coroutine start and perform immediate
            # cleanup work without multiplying the shared shutdown budget.
            return max(0.001, deadline - loop.time())

        # Keep HTTP/WebSocket alive until the active page acknowledges teardown and
        # closes its app window. Otherwise transport loss forces a process-group kill.
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(
                self._controller.close_active_session(),
                timeout=remaining_timeout(),
            )

        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(
                self._http_server.shutdown(),
                timeout=remaining_timeout(),
            )

        if self._http_task is not None:
            done, _pending = await asyncio.wait(
                {self._http_task},
                timeout=remaining_timeout(),
            )
            if not done:
                self._http_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._http_task
            else:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    self._http_task.result()

            self._http_task = None

        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(
                self._socket.stop(),
                timeout=remaining_timeout(),
            )

        await self._remove_signal_handlers()
        self._controller_health_path.unlink(missing_ok=True)
        self._ready.clear()
        self._started = False
        self._running = False

    def _publish_controller_status(self, status: str) -> None:
        marker_status = {
            "idle": "ready",
            "launching": "starting",
            "connecting": "starting",
            "live": "conversation-active",
            "stopping": "starting",
        }.get(status, "error")
        path = self._controller_health_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(f"{marker_status}\n", encoding="utf-8")
        temporary.replace(path)

    async def _install_signal_handlers(self) -> None:
        if self._signal_handlers_registered:
            return

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, self.request_shutdown)
            loop.add_signal_handler(signal.SIGTERM, self.request_shutdown)
            self._signal_handlers_registered = True
        except (RuntimeError, NotImplementedError):
            return

    async def _remove_signal_handlers(self) -> None:
        if not self._signal_handlers_registered:
            return

        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
        self._signal_handlers_registered = False


def main() -> int:
    service = RuntimeService(Settings.from_env())
    try:
        asyncio.run(service.run())
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
