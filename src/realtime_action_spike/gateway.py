"""Loopback FastAPI gateway for OpenAI Realtime SDP and local action execution."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from .capabilities import (
    CAPABILITIES,
    CapabilityBroker,
    ExecutionContractError,
    UnknownCapabilityError,
)
from .config import Settings, build_realtime_session
from .openai.calls import (
    RealtimeCallHandle,
    RealtimeCallHandleParseError,
    parse_realtime_call_handle,
)
from .runtime.browser import NoopBrowserHandle
from .runtime.controller import StaleControlMessage, VoiceSessionController
from .runtime.tokens import LaunchTokenStore

OPENAI_REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"
OPENAI_REALTIME_SESSION_HEADER = "X-OpenAI-Realtime-Session-ID"
MAX_OPENAI_CALLS_PER_SESSION = 512


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


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=200)
    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=128)
    arguments: str | dict[str, Any]


def _openai_execution_fingerprint(execution_request: ExecutionRequest) -> str:
    arguments: Any = execution_request.arguments
    if isinstance(arguments, str):
        with suppress(json.JSONDecodeError):
            arguments = json.loads(arguments)
    canonical_request = json.dumps(
        {"name": execution_request.name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_request.encode()).hexdigest()


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
    voice_css = web_root / "voice.css"
    voice_js = web_root / "voice.js"
    call_handle_registry = call_handle_registry or _InMemoryRealtimeCallHandleRegistry()
    interruption_js = web_root / "interruption_state.mjs"

    class _DiagnosticLauncher:
        def launch(self, loopback_url: str) -> NoopBrowserHandle:
            return NoopBrowserHandle()

    _controller = (
        controller
        if controller is not None
        else VoiceSessionController(
            launcher=_DiagnosticLauncher(),
            token_store=LaunchTokenStore(),
        )
    )

    active_session_id: str | None = None
    latest_session_attempt = 0
    execution_results_by_call_id: dict[str, tuple[str, int, str]] = {}

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
        if asset_name == "voice.css":
            return FileResponse(voice_css, media_type="text/css")
        if asset_name == "voice.js":
            return FileResponse(voice_js, media_type="text/javascript")
        if asset_name == "interruption_state.mjs":
            return FileResponse(interruption_js, media_type="text/javascript")
        return Response(status_code=404)

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

        while True:
            try:
                raw_message = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            try:
                closed = await _controller.process_control_message(session_id, raw_message)
            except (StaleControlMessage, ValueError):
                await websocket.close(code=4403)
                return

            if closed is not None:
                await websocket.send_text(closed.model_dump_json())
                await websocket.close()
                return

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "api_key_configured": settings.api_key_value() is not None,
            "model": settings.realtime_model,
            "capabilities": [capability.name for capability in CAPABILITIES],
            "controller_status": _controller.status,
        }

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

        active_session_id = secrets.token_urlsafe(24)
        execution_results_by_call_id.clear()
        call_handle_registry.clear()
        call_handle_registry.set(active_session_id, handle)

        return Response(
            content=handle.sdp_answer,
            media_type="application/sdp",
            headers={OPENAI_REALTIME_SESSION_HEADER: active_session_id},
        )

    @app.post("/execute")
    async def execute_capability(execution_request: ExecutionRequest) -> Response:
        if active_session_id is None or execution_request.session_id != active_session_id:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "call_id": execution_request.call_id,
                    "error": {
                        "type": "stale_session",
                        "message": "OpenAI Realtime session is not current",
                    },
                },
            )

        fingerprint = _openai_execution_fingerprint(execution_request)
        cached = execution_results_by_call_id.get(execution_request.call_id)
        if cached is not None:
            cached_fingerprint, cached_status, cached_body_json = cached
            if cached_fingerprint != fingerprint:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "call_id": execution_request.call_id,
                        "error": {
                            "type": "call_id_conflict",
                            "message": (
                                "OpenAI call_id was already used with a different request"
                            ),
                        },
                    },
                )
            return Response(
                content=cached_body_json,
                status_code=cached_status,
                media_type="application/json",
            )

        if len(execution_results_by_call_id) >= MAX_OPENAI_CALLS_PER_SESSION:
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "call_id": execution_request.call_id,
                    "error": {
                        "type": "session_call_limit_exceeded",
                        "message": "OpenAI Realtime session reached its execution safety limit",
                    },
                },
            )

        try:
            result = broker.execute(execution_request.name, execution_request.arguments)
        except UnknownCapabilityError as exc:
            status_code = 400
            body = {
                "ok": False,
                "call_id": execution_request.call_id,
                "error": {"type": "unknown_capability", "message": str(exc)},
            }
        except ExecutionContractError as exc:
            status_code = 400
            body = {
                "ok": False,
                "call_id": execution_request.call_id,
                "error": {"type": "invalid_arguments", "message": str(exc)},
            }
        else:
            status_code = 200
            body = {"call_id": execution_request.call_id, **result}

        body_json = json.dumps(body, separators=(",", ":"))
        execution_results_by_call_id[execution_request.call_id] = (
            fingerprint,
            status_code,
            body_json,
        )
        return Response(content=body_json, status_code=status_code, media_type="application/json")

    return app


app = create_app()
