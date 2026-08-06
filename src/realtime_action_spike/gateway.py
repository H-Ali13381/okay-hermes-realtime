"""Loopback FastAPI gateway for OpenAI Realtime SDP and voice-session control."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
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
from .runtime.browser import NoopBrowserHandle
from .runtime.controller import StaleControlMessage, VoiceSessionController
from .runtime.protocol import StopMessage
from .runtime.tokens import LaunchTokenStore

OPENAI_REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"

OPENAI_REALTIME_SESSION_HEADER = "X-OpenAI-Realtime-Session-ID"
LOCAL_CLIENT_HEADER = "X-Okay-Hermes-Client"
LOCAL_CLIENT_HEADER_VALUE = "voice-page-v1"
LOCAL_CONTROLLER_SESSION_HEADER = "X-Okay-Hermes-Session-ID"
LOCAL_EXECUTION_SCOPE_HEADER = "X-Okay-Hermes-Execution-Scope"
MAX_OPENAI_CALLS_PER_SESSION = 512
_LOCAL_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,128}$")
logger = logging.getLogger(__name__)



class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=16, max_length=200)
    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=128)
    arguments: str | dict[str, Any]


def _execution_fingerprint(execution_request: ExecutionRequest) -> str:
    arguments: Any = execution_request.arguments
    if isinstance(arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            arguments = json.loads(arguments)
    canonical_request = json.dumps(
        {"name": execution_request.name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_request.encode()).hexdigest()


class AsyncPostClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...



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


async def _relay_control_websocket(
    websocket: WebSocket,
    controller: VoiceSessionController,
    session_id: str,
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
                    return

                try:
                    closed = await controller.process_control_message(session_id, raw_message)
                except (StaleControlMessage, ValueError) as exc:
                    logger.warning(
                        "control_message_rejected %s",
                        json.dumps(
                            {
                                "session_id": session_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "message_prefix": raw_message[:200],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
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
) -> FastAPI:
    settings = settings or Settings.from_env()
    broker = broker or CapabilityBroker()
    web_root = Path(__file__).resolve().parent / "web"
    index_html = web_root / "index.html"

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
    active_execution_scope: str | None = None
    execution_results_by_call_id: dict[str, tuple[str, int, str]] = {}
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

        await _relay_control_websocket(websocket, _controller, session_id)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "api_key_configured": settings.api_key_value() is not None,
            "model": settings.realtime_model,
            "capabilities": [capability.name for capability in CAPABILITIES],
            "controller_status": _controller.status,
        }

    @app.post("/execute")
    async def execute_capability(
        execution_request: ExecutionRequest,
        request: Request,
    ) -> Response:
        if not _is_loopback_client(request):
            raise HTTPException(status_code=403, detail="Only local clients can execute tools")
        if request.headers.get(LOCAL_CLIENT_HEADER) != LOCAL_CLIENT_HEADER_VALUE:
            raise HTTPException(status_code=403, detail="Invalid local voice client")
        if (
            active_execution_scope is None
            or execution_request.scope != active_execution_scope
            or active_session_id is None
            or _controller.active_session_id != active_session_id
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "call_id": execution_request.call_id,
                    "error": {
                        "type": "stale_session",
                        "message": "Local Realtime execution scope is not current",
                    },
                },
            )

        fingerprint = _execution_fingerprint(execution_request)
        cached = execution_results_by_call_id.get(execution_request.call_id)
        if cached is not None:
            cached_fingerprint, cached_status, cached_body = cached
            if cached_fingerprint != fingerprint:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "call_id": execution_request.call_id,
                        "error": {
                            "type": "call_id_conflict",
                            "message": "OpenAI call_id was reused with a different request",
                        },
                    },
                )
            return Response(
                content=cached_body,
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
                        "message": "Realtime session reached its execution safety limit",
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

    @app.post("/session")
    async def create_realtime_session(request: Request) -> Response:
        nonlocal active_execution_scope, active_session_id, latest_session_attempt
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
            if request.headers.get(LOCAL_CLIENT_HEADER) != LOCAL_CLIENT_HEADER_VALUE:
                raise HTTPException(status_code=403, detail="Invalid local voice client")
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

        active_session_id = local_session_id or secrets.token_urlsafe(24)
        active_execution_scope = secrets.token_urlsafe(24) if local_session_id is not None else None
        execution_results_by_call_id.clear()

        headers = {OPENAI_REALTIME_SESSION_HEADER: active_session_id}
        if active_execution_scope is not None:
            headers[LOCAL_EXECUTION_SCOPE_HEADER] = active_execution_scope
        return Response(
            content=upstream.text,
            media_type="application/sdp",
            headers=headers,
        )

    return app


app = create_app()
