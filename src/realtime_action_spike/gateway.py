"""Loopback FastAPI gateway for OpenAI Realtime SDP and voice-session control."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from .capabilities import (
    CAPABILITIES,
    CapabilityBroker,
)
from .config import Settings, build_realtime_session
from .openai.calls import (
    RealtimeCallHandle,
    RealtimeCallHandleParseError,
    parse_realtime_call_handle,
)
from .runtime.browser import NoopBrowserHandle
from .runtime.controller import StaleControlMessage, VoiceSessionController
from .runtime.protocol import (
    RealtimeConnectedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    parse_loopback_message,
)
from .runtime.tokens import LaunchTokenStore

OPENAI_REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"
OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_REALTIME_SESSION_HEADER = "X-OpenAI-Realtime-Session-ID"
LOCAL_CLIENT_HEADER = "X-Okay-Hermes-Client"
LOCAL_CLIENT_HEADER_VALUE = "voice-page-v1"
LOCAL_CONTROLLER_SESSION_HEADER = "X-Okay-Hermes-Session-ID"
_LOCAL_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,128}$")
logger = logging.getLogger(__name__)


def _safe_http_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return status_code
    return None


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


class RealtimeCallHandleRegistry(Protocol):
    def get(self, local_session_id: str) -> RealtimeCallHandle | None:
        ...

    def set(self, local_session_id: str, handle: RealtimeCallHandle) -> None:
        ...

    def clear(self) -> None:
        ...


class _InMemoryRealtimeCallHandleRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, RealtimeCallHandle] = {}

    def get(self, local_session_id: str) -> RealtimeCallHandle | None:
        return self._handles.get(local_session_id)

    def set(self, local_session_id: str, handle: RealtimeCallHandle) -> None:
        self._handles = {local_session_id: handle}

    def clear(self) -> None:
        self._handles.clear()


def _safety_identifier() -> str:
    value = b"openai-realtime-action-spike:single-local-user"
    return hashlib.sha256(value).hexdigest()


async def _post_to_openai(
    *,
    settings: Settings,
    sdp: str,
    upstream_client: AsyncPostClient | None,
) -> httpx.Response:
    api_key = settings.api_key_value()
    assert api_key is not None

    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Safety-Identifier": _safety_identifier(),
        },
        "files": {
            "sdp": (None, sdp, "application/sdp"),
            "session": (
                None,
                json.dumps(build_realtime_session(settings), separators=(",", ":")),
                "application/json",
            ),
        },
    }

    if upstream_client is not None:
        return await upstream_client.post(OPENAI_REALTIME_CALLS_URL, **request_kwargs)

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        return await client.post(OPENAI_REALTIME_CALLS_URL, **request_kwargs)


async def _post_client_secret(
    *,
    settings: Settings,
    session: dict[str, Any],
    upstream_client: AsyncPostClient | None,
) -> httpx.Response:
    api_key = settings.api_key_value()
    assert api_key is not None
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Safety-Identifier": _safety_identifier(),
            "Content-Type": "application/json",
        },
        "json": {"session": session},
    }
    if upstream_client is not None:
        return await upstream_client.post(OPENAI_REALTIME_CLIENT_SECRETS_URL, **request_kwargs)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        return await client.post(OPENAI_REALTIME_CLIENT_SECRETS_URL, **request_kwargs)


async def _relay_control_websocket(
    websocket: WebSocket,
    controller: VoiceSessionController,
    session_id: str,
    *,
    bind_realtime_call: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    async def send_text(payload: str) -> bool:
        try:
            await websocket.send_text(payload)
        except (RuntimeError, WebSocketDisconnect):
            return False
        return True

    receive_task = asyncio.create_task(websocket.receive_text())
    outbound_task = asyncio.create_task(controller.wait_for_outbound_message(session_id))
    server_stop_sent = False
    realtime_call_bound = False
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, outbound_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                try:
                    raw_message = receive_task.result()
                except WebSocketDisconnect:
                    with contextlib.suppress(StaleControlMessage):
                        await controller.request_teardown(
                            session_id,
                            outcome=SessionOutcome.FAILED,
                            reason=StopReason.TRANSPORT_FAILURE,
                            error="control websocket disconnected",
                        )
                    return

                try:
                    message = parse_loopback_message(raw_message)
                    if isinstance(message, RealtimeConnectedMessage):
                        if bind_realtime_call is None or realtime_call_bound:
                            raise ValueError("realtime call binder is unavailable")
                        try:
                            await bind_realtime_call(session_id, message.provider_call_id)
                        except Exception as exc:
                            logger.error(
                                "realtime sideband binding failed session_id=%s "
                                "error_type=%s status_code=%s",
                                session_id,
                                type(exc).__name__,
                                _safe_http_status(exc),
                            )
                            with contextlib.suppress(StaleControlMessage, TimeoutError):
                                await controller.request_teardown(
                                    session_id,
                                    outcome=SessionOutcome.FAILED,
                                    reason=StopReason.TRANSPORT_FAILURE,
                                    error="sideband connection failed",
                                )
                            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                                await websocket.close(code=1011)
                            return
                        realtime_call_bound = True
                    closed = await controller.process_control_message(session_id, raw_message)
                except (StaleControlMessage, ValueError):
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close(code=4403)
                    return

                if closed is not None:
                    deadline = asyncio.get_running_loop().time() + 0.1
                    while not server_stop_sent:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            outbound = await asyncio.wait_for(
                                asyncio.shield(outbound_task),
                                timeout=remaining,
                            )
                        except (TimeoutError, StaleControlMessage):
                            break
                        if not await send_text(outbound.model_dump_json()):
                            return
                        server_stop_sent = isinstance(outbound, StopMessage)
                        if not server_stop_sent:
                            outbound_task = asyncio.create_task(
                                controller.wait_for_outbound_message(session_id)
                            )
                    if not await send_text(closed.model_dump_json()):
                        return
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close()
                    return
                receive_task = asyncio.create_task(websocket.receive_text())

            if outbound_task in done:
                try:
                    outbound = outbound_task.result()
                except StaleControlMessage:
                    return
                if not await send_text(outbound.model_dump_json()):
                    return
                server_stop_sent = server_stop_sent or isinstance(outbound, StopMessage)
                outbound_task = asyncio.create_task(
                    controller.wait_for_outbound_message(session_id)
                )
    finally:
        receive_task.cancel()
        outbound_task.cancel()
        await asyncio.gather(receive_task, outbound_task, return_exceptions=True)


def create_app(
    settings: Settings | None = None,
    *,
    upstream_client: AsyncPostClient | None = None,
    broker: CapabilityBroker | None = None,
    controller: VoiceSessionController | None = None,
    call_handle_registry: RealtimeCallHandleRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    broker = broker or CapabilityBroker()
    web_root = Path(__file__).resolve().parent / "web"
    index_html = web_root / "index.html"
    call_handle_registry = call_handle_registry or _InMemoryRealtimeCallHandleRegistry()
    web_assets = {
        "voice.css": "text/css",
        "voice.js": "text/javascript",
    }

    class _DiagnosticLauncher:
        def launch(self, loopback_url: str) -> NoopBrowserHandle:
            return NoopBrowserHandle()

    _controller = (
        controller
        if controller is not None
        else VoiceSessionController(
            launcher=_DiagnosticLauncher(),
            token_store=LaunchTokenStore(),
            capability_broker=broker,
        )
    )

    active_session_id: str | None = None
    latest_session_attempt = 0

    def _is_loopback_client(request: Request) -> bool:
        if request.client is None:
            return False
        return request.client.host in {"127.0.0.1", "::1", "localhost", "testclient"}

    def _render_voice_page(session_id: str | None = None) -> HTMLResponse:
        if session_id is None:
            return HTMLResponse(content=index_html.read_text(encoding="utf-8"))

        html = index_html.read_text(encoding="utf-8")
        marker = f"<script>window.__LOCAL_SESSION_ID__ = {json.dumps(session_id)};</script>\n"
        return HTMLResponse(content=html.replace("</head>", marker + "</head>", 1))

    app = FastAPI(title="OpenAI Realtime Action Spike Gateway", version="0.1.0")
    app.state.get_realtime_call_handle = call_handle_registry.get

    @app.get("/voice", include_in_schema=False)
    async def voice_page(activation: str | None = None) -> HTMLResponse:
        if activation is None:
            return _render_voice_page()

        session_id = _controller.validate_activation_token(activation)
        if session_id is None:
            raise HTTPException(status_code=403, detail="Invalid or expired activation token")

        return _render_voice_page(session_id=session_id)

    @app.get("/assets/{asset_name}", include_in_schema=False)
    async def voice_asset(asset_name: str) -> Response:
        media_type = web_assets.get(asset_name)
        if media_type is None:
            return Response(status_code=404)
        return FileResponse(web_root / asset_name, media_type=media_type)

    @app.post("/internal/open")
    async def open_internal(request: Request) -> dict[str, str]:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Only local clients can open voice pages")

        result = await _controller.activate(str(request.url_for("voice_page")))
        return {"status": result.status}

    @app.websocket("/control")
    async def control(websocket: WebSocket, activation: str | None = None) -> None:
        await websocket.accept()

        if activation is None:
            await websocket.close(code=4403)
            return

        session_id = await _controller.consume_activation_token(activation)
        if session_id is None:
            await websocket.close(code=4403)
            return

        async def bind_realtime_call(local_session_id: str, provider_call_id: str) -> None:
            api_key = settings.api_key_value()
            if api_key is None:
                raise ValueError("OPENAI_API_KEY is not configured")
            try:
                await _controller.start_realtime_sideband(
                    local_session_id=local_session_id,
                    call_id=provider_call_id,
                    api_key=api_key,
                )
            except StaleControlMessage as exc:
                raise ValueError("Local voice session is no longer active") from exc

        await _relay_control_websocket(
            websocket,
            _controller,
            session_id,
            bind_realtime_call=bind_realtime_call,
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "api_key_configured": settings.api_key_value() is not None,
            "model": settings.realtime_model,
            "capabilities": [capability.name for capability in CAPABILITIES],
            "controller_status": _controller.status,
        }

    @app.post("/client-secret")
    async def create_client_secret(request: Request) -> Response:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Only local clients can create sessions")
        if request.headers.get(LOCAL_CLIENT_HEADER) != LOCAL_CLIENT_HEADER_VALUE:
            raise HTTPException(status_code=403, detail="Invalid local voice client")
        if settings.api_key_value() is None:
            raise HTTPException(
                status_code=503,
                detail="OPENAI_API_KEY is not configured on the gateway",
            )

        local_session_id = request.headers.get(LOCAL_CONTROLLER_SESSION_HEADER)
        if local_session_id is not None:
            if _LOCAL_SESSION_ID_RE.fullmatch(local_session_id) is None:
                raise HTTPException(status_code=400, detail="Invalid local session binding")
            if _controller.active_session_id != local_session_id:
                raise HTTPException(
                    status_code=409,
                    detail="Local voice session is no longer active",
                )

        session = build_realtime_session(settings)
        if local_session_id is None:
            session = {
                **session,
                "instructions": (
                    f"{session['instructions']}\n\n"
                    "Manual diagnostic mode cannot execute tools or external actions. "
                    "Respond conversationally and do not claim to perform actions."
                ),
                "tools": [],
                "tool_choice": "none",
            }

        try:
            upstream = await _post_client_secret(
                settings=settings,
                session=session,
                upstream_client=upstream_client,
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=502,
                content={"detail": "OpenAI Realtime client secret request failed"},
            )
        if not upstream.is_success:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "OpenAI Realtime client secret creation failed",
                    "upstream_status": upstream.status_code,
                },
            )

        try:
            payload = upstream.json()
            value = payload["value"]
            expires_at = payload["expires_at"]
            if not isinstance(value, str) or not value.startswith("ek_"):
                raise ValueError
            if not isinstance(expires_at, int):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return JSONResponse(
                status_code=502,
                content={"detail": "OpenAI returned an invalid client secret"},
            )

        return JSONResponse(
            content={
                "value": value,
                "expires_at": expires_at,
                "session": session,
            }
        )

    @app.post("/session")
    async def create_realtime_session(request: Request) -> Response:
        nonlocal active_session_id, latest_session_attempt
        if settings.api_key_value() is None:
            raise HTTPException(
                status_code=503,
                detail="OPENAI_API_KEY is not configured on the gateway",
            )

        media_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if media_type != "application/sdp":
            raise HTTPException(status_code=415, detail="Content-Type must be application/sdp")

        header_session_id = request.headers.get(LOCAL_CONTROLLER_SESSION_HEADER)
        query_session_id = request.query_params.get("local_session_id")
        if (
            header_session_id is not None
            and query_session_id is not None
            and header_session_id != query_session_id
        ):
            raise HTTPException(status_code=400, detail="Conflicting local session bindings")

        if query_session_id is not None:
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ek_"):
                raise HTTPException(status_code=403, detail="Invalid Realtime client credential")
            if _LOCAL_SESSION_ID_RE.fullmatch(query_session_id) is None:
                raise HTTPException(status_code=400, detail="Invalid local session binding")
            if _controller.active_session_id != query_session_id:
                raise HTTPException(
                    status_code=409,
                    detail="Local voice session is no longer active",
                )

        local_session_id = query_session_id or header_session_id
        if (
            local_session_id is not None
            and _LOCAL_SESSION_ID_RE.fullmatch(local_session_id) is None
        ):
            raise HTTPException(status_code=400, detail="Invalid local session binding")

        try:
            sdp = (await request.body()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="SDP must be UTF-8 text") from exc
        if not sdp.strip():
            raise HTTPException(status_code=400, detail="SDP offer is empty")

        latest_session_attempt += 1
        session_attempt = latest_session_attempt
        try:
            upstream = await _post_to_openai(
                settings=settings,
                sdp=sdp,
                upstream_client=upstream_client,
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "OpenAI Realtime session request failed",
                    "upstream_status": None,
                    "request_id": None,
                },
            )

        if session_attempt != latest_session_attempt:
            return JSONResponse(
                status_code=409,
                content={"detail": "OpenAI Realtime session attempt was superseded"},
            )

        if not upstream.is_success:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "OpenAI Realtime session creation failed",
                    "upstream_status": upstream.status_code,
                },
            )

        try:
            handle = parse_realtime_call_handle(
                location=upstream.headers.get("Location"),
                headers=upstream.headers,
                sdp_answer=upstream.text,
            )
        except RealtimeCallHandleParseError:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "OpenAI Realtime session creation failed",
                },
            )

        if local_session_id is not None:
            api_key = settings.api_key_value()
            assert api_key is not None
            try:
                await _controller.start_realtime_sideband(
                    local_session_id=local_session_id,
                    call_id=handle.call_id,
                    api_key=api_key,
                )
            except StaleControlMessage:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "Local voice session is no longer active"},
                )
            except Exception:
                return JSONResponse(
                    status_code=502,
                    content={"detail": "OpenAI Realtime sideband connection failed"},
                )

        active_session_id = local_session_id or secrets.token_urlsafe(24)
        call_handle_registry.clear()
        call_handle_registry.set(active_session_id, handle)

        return Response(
            content=handle.sdp_answer,
            media_type="application/sdp",
            headers={OPENAI_REALTIME_SESSION_HEADER: active_session_id},
        )

    return app


app = create_app()
