from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from realtime_action_spike.config import Settings, build_realtime_session
from realtime_action_spike.gateway import (
    LOCAL_CLIENT_HEADER,
    LOCAL_CLIENT_HEADER_VALUE,
    LOCAL_CONTROLLER_SESSION_HEADER,
    OPENAI_REALTIME_CALLS_URL,
    OPENAI_REALTIME_CLIENT_SECRETS_URL,
    _relay_control_websocket,
    create_app,
)
from realtime_action_spike.openai.calls import RealtimeCallHandle
from realtime_action_spike.runtime.browser import NoopBrowserHandle
from realtime_action_spike.runtime.controller import VoiceSessionController
from realtime_action_spike.runtime.protocol import (
    ActionStateMessage,
    LoopbackMessage,
    PageReadyMessage,
    PageStartedMessage,
    RealtimeConnectedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    encode_loopback_message,
)
from realtime_action_spike.runtime.tokens import LaunchTokenStore


class StubUpstreamClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


class OutOfOrderUpstreamClient:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.call_count = 0

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        self.call_count += 1
        call_number = self.call_count
        if call_number == 1:
            self.first_started.set()
            await self.release_first.wait()
        return httpx.Response(
            201,
            text=f"v=0\r\nanswer-{call_number}",
            headers={"Location": f"/v1/realtime/calls/call-{call_number}"},
        )


class CapturingLauncher:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.handles: list[NoopBrowserHandle] = []

    def launch(self, loopback_url: str) -> NoopBrowserHandle:
        self.urls.append(loopback_url)
        handle = NoopBrowserHandle()
        self.handles.append(handle)
        return handle


class CallHandleRegistry:
    def __init__(self) -> None:
        self.calls: dict[str, RealtimeCallHandle] = {}

    def get(self, local_session_id: str) -> RealtimeCallHandle | None:
        return self.calls.get(local_session_id)

    def set(self, local_session_id: str, handle: RealtimeCallHandle) -> None:
        self.calls = {local_session_id: handle}

    def clear(self) -> None:
        self.calls.clear()


class SidebandStartController:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def start_realtime_sideband(
        self,
        *,
        local_session_id: str,
        call_id: str,
        api_key: str,
    ) -> None:
        self.calls.append(
            {
                "local_session_id": local_session_id,
                "call_id": call_id,
                "api_key": api_key,
            }
        )


class OutboundController:
    def __init__(self, message: LoopbackMessage) -> None:
        self.message = message
        self.delivered = False

    async def wait_for_outbound_message(self, _session_id: str) -> LoopbackMessage:
        if not self.delivered:
            self.delivered = True
            return self.message
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def process_control_message(self, _session_id: str, _raw: str) -> None:
        return None


class BlockingControlWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.message_sent = asyncio.Event()

    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)
        self.message_sent.set()


class DisconnectingControlWebSocket:
    async def receive_text(self) -> str:
        raise WebSocketDisconnect()

    async def send_text(self, _payload: str) -> None:
        raise AssertionError("disconnect path must not send")


class DisconnectController:
    def __init__(self) -> None:
        self.teardown_calls: list[dict[str, object]] = []
        self._never = asyncio.Event()

    async def wait_for_outbound_message(self, _session_id: str) -> LoopbackMessage:
        await self._never.wait()
        raise AssertionError("unreachable")

    async def request_teardown(self, session_id: str, **kwargs: object) -> None:
        self.teardown_calls.append({"session_id": session_id, **kwargs})


class RealtimeBindingWebSocket:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.received = False
        self.close_codes: list[int] = []

    async def receive_text(self) -> str:
        if not self.received:
            self.received = True
            return self.payload
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_text(self, _payload: str) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


class BindingOrderController:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.processed = asyncio.Event()

    async def wait_for_outbound_message(self, _session_id: str) -> LoopbackMessage:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def process_control_message(self, _session_id: str, _raw: str) -> None:
        self.events.append("processed")
        self.processed.set()


class BindingFailureController(BindingOrderController):
    def __init__(self) -> None:
        super().__init__([])
        self.teardown_calls: list[dict[str, object]] = []

    async def request_teardown(self, session_id: str, **kwargs: object) -> None:
        self.teardown_calls.append({"session_id": session_id, **kwargs})


class RejectedSidebandError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.response = type("Response", (), {"status_code": status_code})()
        super().__init__("provider rejected secret-value")


class DisconnectingSendWebSocket(BlockingControlWebSocket):
    async def send_text(self, payload: str) -> None:
        del payload
        raise WebSocketDisconnect()


def settings(api_key: str | None = "test-secret-key") -> Settings:
    return Settings(openai_api_key=api_key)


def start_openai_realtime_session(client: TestClient, offer: str = "mock-offer") -> str:
    response = client.post(
        "/session",
        content=f"v=0\r\n{offer}",
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 200
    session_id = response.headers.get("x-openai-realtime-session-id")
    assert session_id
    return session_id


def test_session_instructions_decline_actions_without_a_supplied_tool() -> None:
    session = build_realtime_session(settings())
    instructions = session["instructions"]

    assert isinstance(instructions, str)
    assert "If no supplied tool can perform a requested side effect" in instructions
    assert (
        "Call a tool when the user explicitly requests current time, a timer, media"
        not in instructions
    )


def test_session_configuration_uses_fast_natural_voice_defaults() -> None:
    session = build_realtime_session(settings())

    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1-mini"
    assert session["reasoning"] == {"effort": "minimal"}
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe"}
    assert session["tool_choice"] == "auto"
    assert len(session["tools"]) == 2
    assert "Do not claim an action succeeded before its tool result" in session["instructions"]


def test_health_reports_model_and_missing_key_without_secret_material() -> None:
    client = TestClient(create_app(settings(api_key=None)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "api_key_configured": False,
        "model": "gpt-realtime-2.1-mini",
        "capabilities": [
            "assistant_get_current_time",
            "voice_end_session",
        ],
        "controller_status": "idle",
    }


def test_session_endpoint_requires_server_side_api_key() -> None:
    client = TestClient(create_app(settings(api_key=None)))

    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={"Content-Type": "application/sdp"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "OPENAI_API_KEY is not configured on the gateway"


def test_client_secret_endpoint_mints_ephemeral_token_with_server_owned_session() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            200,
            json={
                "value": "ek_ephemeral_browser_token",
                "expires_at": 1_796_000_000,
                "session": {"type": "realtime"},
            },
        )
    )
    controller, session_id, _token = _activated_controller()
    client = TestClient(create_app(settings(), upstream_client=upstream, controller=controller))

    response = client.post(
        "/client-secret",
        headers={
            LOCAL_CLIENT_HEADER: LOCAL_CLIENT_HEADER_VALUE,
            LOCAL_CONTROLLER_SESSION_HEADER: session_id,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "value": "ek_ephemeral_browser_token",
        "expires_at": 1_796_000_000,
        "session": build_realtime_session(settings()),
    }
    assert "test-secret-key" not in response.text
    assert upstream.calls == [
        {
            "url": OPENAI_REALTIME_CLIENT_SECRETS_URL,
            "headers": {
                "Authorization": "Bearer test-secret-key",
                "OpenAI-Safety-Identifier": upstream.calls[0]["headers"][
                    "OpenAI-Safety-Identifier"
                ],
                "Content-Type": "application/json",
            },
            "json": {"session": build_realtime_session(settings())},
        }
    ]


def test_client_secret_rejects_stale_controller_session() -> None:
    controller, _session_id, _token = _activated_controller()
    client = TestClient(create_app(settings(), controller=controller))

    response = client.post(
        "/client-secret",
        headers={
            LOCAL_CLIENT_HEADER: LOCAL_CLIENT_HEADER_VALUE,
            LOCAL_CONTROLLER_SESSION_HEADER: "local-session-stale",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Local voice session is no longer active"}


def test_client_secret_rejects_requests_without_voice_client_header() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            200,
            json={"value": "ek_test_ephemeral", "expires_at": 1_800_000_000},
        )
    )

    response = TestClient(create_app(settings(), upstream_client=upstream)).post(
        "/client-secret"
    )

    assert response.status_code == 403
    assert upstream.calls == []


def test_manual_client_secret_disables_tools_without_sideband() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            200,
            json={"value": "ek_manual_ephemeral", "expires_at": 1_800_000_000},
        )
    )

    response = TestClient(create_app(settings(), upstream_client=upstream)).post(
        "/client-secret",
        headers={LOCAL_CLIENT_HEADER: LOCAL_CLIENT_HEADER_VALUE},
    )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["tools"] == []
    assert session["tool_choice"] == "none"
    assert "Manual diagnostic mode cannot execute tools" in session["instructions"]
    assert upstream.calls[0]["json"] == {"session": session}


def test_session_endpoint_rejects_wrong_media_type() -> None:
    client = TestClient(create_app(settings()))

    response = client.post("/session", content="v=0\r\nmock-offer")

    assert response.status_code == 415


def test_session_endpoint_relays_sdp_and_server_owned_configuration() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            201,
            text="v=0\r\nmock-answer",
            headers={
                "Location": "/v1/realtime/calls/call_endpoint",
                "x-request-id": "req_realtime_test",
            },
        )
    )
    registry = CallHandleRegistry()
    client = TestClient(
        create_app(settings(), upstream_client=upstream, call_handle_registry=registry)
    )

    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={
            "Content-Type": "application/sdp",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["x-openai-realtime-session-id"]
    assert "x-openai-request-id" not in response.headers
    assert response.text == "v=0\r\nmock-answer"
    assert len(upstream.calls) == 1

    session_id = response.headers["x-openai-realtime-session-id"]
    handle = registry.get(session_id)
    assert handle is not None
    assert handle.call_id == "call_endpoint"
    assert handle.request_id == "req_realtime_test"
    assert handle.sdp_answer == "v=0\r\nmock-answer"

    call = upstream.calls[0]
    assert call["url"] == OPENAI_REALTIME_CALLS_URL
    assert call["headers"]["Authorization"] == "Bearer test-secret-key"
    assert call["headers"]["OpenAI-Safety-Identifier"]
    assert call["files"]["sdp"] == (None, "v=0\r\nmock-offer", "application/sdp")
    session_json = call["files"]["session"][1]
    assert json.loads(session_json)["model"] == "gpt-realtime-2.1-mini"


def test_session_endpoint_starts_sideband_for_exact_controller_session() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            201,
            text="v=0\r\nmock-answer",
            headers={"Location": "/v1/realtime/calls/call_sideband"},
        )
    )
    registry = CallHandleRegistry()
    controller = SidebandStartController()
    client = TestClient(
        create_app(
            settings(),
            upstream_client=upstream,
            call_handle_registry=registry,
            controller=controller,  # type: ignore[arg-type]
        )
    )

    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={
            "Content-Type": "application/sdp",
            "X-Okay-Hermes-Session-ID": "local-session-1234",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-openai-realtime-session-id"] == "local-session-1234"
    assert controller.calls == [
        {
            "local_session_id": "local-session-1234",
            "call_id": "call_sideband",
            "api_key": "test-secret-key",
        }
    ]
    handle = registry.get("local-session-1234")
    assert handle is not None
    assert handle.call_id == "call_sideband"
    assert "test-secret-key" not in response.text
    assert "call_sideband" not in response.text


def test_session_binding_rejects_malformed_location_without_clearing_existing_binding() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            201,
            text="v=0\r\nmock-answer",
            headers={"Location": "/v1/realtime/calls/call_current"},
        )
    )
    registry = CallHandleRegistry()
    client = TestClient(
        create_app(settings(), upstream_client=upstream, call_handle_registry=registry)
    )
    session_id = start_openai_realtime_session(client, "first-offer")
    bound_before = registry.get(session_id)
    assert bound_before is not None
    assert bound_before.call_id == "call_current"

    upstream.response = httpx.Response(
        201,
        text="v=0\r\nmalformed-answer",
        headers={},
    )
    second = client.post(
        "/session",
        content="v=0\r\nbad-offer",
        headers={"Content-Type": "application/sdp"},
    )
    after_failure = registry.get(session_id)

    assert second.status_code == 502
    assert after_failure is not None
    assert after_failure.call_id == "call_current"
    assert second.json()["detail"] == "OpenAI Realtime session creation failed"
    assert "call_current" not in second.text
    assert "Location" not in second.text


def test_upstream_failure_is_sanitized_and_preserves_request_id() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            401,
            text='{"error":{"message":"bad key test-secret-key"}}',
            headers={"x-request-id": "req_failed_test"},
        )
    )
    client = TestClient(create_app(settings(), upstream_client=upstream))

    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={"Content-Type": "application/sdp"},
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "OpenAI Realtime session creation failed",
        "upstream_status": 401,
    }
    assert "test-secret-key" not in response.text
    assert "req_failed_test" not in response.text


def test_older_session_completion_cannot_replace_newer_openai_scope() -> None:
    async def run_scenario() -> None:
        upstream = OutOfOrderUpstreamClient()
        app = create_app(settings(), upstream_client=upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_task = asyncio.create_task(
                client.post(
                    "/session",
                    content="v=0\r\nfirst-offer",
                    headers={"Content-Type": "application/sdp"},
                )
            )
            await upstream.first_started.wait()
            second = await client.post(
                "/session",
                content="v=0\r\nsecond-offer",
                headers={"Content-Type": "application/sdp"},
            )
            upstream.release_first.set()
            first = await first_task

        assert second.status_code == 200
        assert first.status_code == 409
        assert first.json()["detail"] == "OpenAI Realtime session attempt was superseded"

    asyncio.run(run_scenario())


def test_execute_endpoint_is_not_exposed() -> None:
    client = TestClient(create_app(settings()))

    response = client.post(
        "/execute",
        json={
            "session_id": "retired-browser-authority",
            "call_id": "call_retired",
            "name": "assistant_get_current_time",
            "arguments": {},
        },
    )

    assert response.status_code == 404


def test_same_origin_requests_require_no_cross_origin_cors_headers() -> None:
    client = TestClient(create_app(settings()))

    same_origin = client.options(
        "/session",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "Access-Control-Request-Method": "POST",
        },
    )
    cross_origin = client.options(
        "/execute",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "access-control-allow-origin" not in same_origin.headers
    assert "access-control-allow-origin" not in cross_origin.headers


def _activated_controller() -> tuple[VoiceSessionController, str, str]:
    launcher = CapturingLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=iter(["launch-token-01"]).__next__),
        session_id_factory=iter(["local-session-01"]).__next__,
    )
    result = asyncio.run(controller.activate("http://127.0.0.1:8765/voice"))
    assert result.session_id == "local-session-01"
    assert result.token == "launch-token-01"
    return controller, result.session_id, result.token


def test_voice_activation_token_is_validated_without_consuming() -> None:
    controller, session_id, token = _activated_controller()
    client = TestClient(create_app(settings(), controller=controller))

    manual = client.get("/voice")
    valid = client.get("/voice", params={"activation": token})
    invalid = client.get("/voice", params={"activation": "wrong-token"})

    assert manual.status_code == 200
    assert "__LOCAL_SESSION_ID__" not in manual.text
    assert valid.status_code == 200
    assert f'window.__LOCAL_SESSION_ID__ = "{session_id}"' in valid.text
    assert invalid.status_code == 403
    assert controller.validate_activation_token(token) == session_id


def test_control_websocket_consumes_token_and_closes_session() -> None:
    controller, session_id, token = _activated_controller()
    client = TestClient(create_app(settings(), controller=controller))

    with client.websocket_connect(f"/control?activation={token}") as websocket:
        websocket.send_text(
            encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
        )
        websocket.send_text(
            encode_loopback_message(PageStartedMessage(type="page_started", session_id=session_id))
        )
        websocket.send_text(
            encode_loopback_message(
                StopMessage(type="stop", session_id=session_id, reason=StopReason.BUTTON)
            )
        )
        websocket.send_text(
            encode_loopback_message(
                TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
            )
        )
        server_stop = websocket.receive_json()
        closed = websocket.receive_json()

    assert server_stop == {
        "type": "stop",
        "session_id": session_id,
        "reason": "button",
    }
    assert closed == {
        "type": "session_closed",
        "session_id": session_id,
        "outcome": "completed",
    }
    assert controller.status == "idle"

    with (
        client.websocket_connect(f"/control?activation={token}") as replay,
        pytest.raises(WebSocketDisconnect) as raised,
    ):
        replay.receive_text()
    assert raised.value.code == 4403


@pytest.mark.asyncio
async def test_control_relay_delivers_action_state_without_browser_traffic() -> None:
    action = ActionStateMessage(
        type="action_state",
        session_id="local-session-01",
        capability="assistant_get_current_time",
        state="completed",
        message="Current time retrieved",
    )
    controller = OutboundController(action)
    websocket = BlockingControlWebSocket()
    relay = asyncio.create_task(
        _relay_control_websocket(
            websocket,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            "local-session-01",
        )
    )

    try:
        await asyncio.wait_for(websocket.message_sent.wait(), timeout=1.0)
        assert websocket.sent == [action.model_dump_json()]
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


@pytest.mark.asyncio
async def test_control_relay_delivers_server_stop_without_blocking_receive() -> None:
    stop = StopMessage(
        type="stop",
        session_id="local-relay-stop-01",
        reason=StopReason.NATIVE_CANCEL,
    )
    controller = OutboundController(stop)
    websocket = BlockingControlWebSocket()

    relay = asyncio.create_task(
        _relay_control_websocket(
            websocket,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            "local-relay-stop-01",
        )
    )
    try:
        await asyncio.wait_for(websocket.message_sent.wait(), timeout=1.0)
        assert websocket.sent == [stop.model_dump_json()]
        assert not relay.done()
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


@pytest.mark.asyncio
async def test_control_relay_binds_sideband_before_processing_realtime_connected() -> None:
    events: list[str] = []
    controller = BindingOrderController(events)
    message = RealtimeConnectedMessage(
        type="realtime_connected",
        session_id="local-binding-01",
        provider_call_id="call_provider_01",
    )
    websocket = RealtimeBindingWebSocket(encode_loopback_message(message))

    async def bind(local_session_id: str, provider_call_id: str) -> None:
        assert local_session_id == "local-binding-01"
        assert provider_call_id == "call_provider_01"
        events.append("bound")

    relay = asyncio.create_task(
        _relay_control_websocket(
            websocket,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            "local-binding-01",
            bind_realtime_call=bind,
        )
    )
    try:
        await asyncio.wait_for(controller.processed.wait(), timeout=1.0)
        assert events == ["bound", "processed"]
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


@pytest.mark.asyncio
async def test_control_relay_treats_disconnect_during_send_as_terminal() -> None:
    action = ActionStateMessage(
        type="action_state",
        session_id="local-send-close-01",
        capability="assistant_get_current_time",
        state="completed",
        message="Current time retrieved",
    )

    await asyncio.wait_for(
        _relay_control_websocket(
            DisconnectingSendWebSocket(),  # type: ignore[arg-type]
            OutboundController(action),  # type: ignore[arg-type]
            "local-send-close-01",
        ),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_control_relay_tears_down_when_sideband_binding_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    controller = BindingFailureController()
    message = RealtimeConnectedMessage(
        type="realtime_connected",
        session_id="local-binding-failure-01",
        provider_call_id="call_provider_failure_01",
    )
    websocket = RealtimeBindingWebSocket(encode_loopback_message(message))

    async def fail_binding(_local_session_id: str, _provider_call_id: str) -> None:
        raise RejectedSidebandError(403)

    await asyncio.wait_for(
        _relay_control_websocket(
            websocket,  # type: ignore[arg-type]
            controller,  # type: ignore[arg-type]
            "local-binding-failure-01",
            bind_realtime_call=fail_binding,
        ),
        timeout=1.0,
    )

    assert websocket.close_codes == [1011]
    assert controller.teardown_calls == [
        {
            "session_id": "local-binding-failure-01",
            "outcome": SessionOutcome.FAILED,
            "reason": StopReason.TRANSPORT_FAILURE,
            "error": "sideband connection failed",
        }
    ]
    assert "error_type=RejectedSidebandError status_code=403" in caplog.text
    assert "secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_control_disconnect_requests_failed_transport_teardown() -> None:
    controller = DisconnectController()

    await _relay_control_websocket(
        DisconnectingControlWebSocket(),  # type: ignore[arg-type]
        controller,  # type: ignore[arg-type]
        "local-disconnect-01",
    )

    assert controller.teardown_calls == [
        {
            "session_id": "local-disconnect-01",
            "outcome": SessionOutcome.FAILED,
            "reason": StopReason.TRANSPORT_FAILURE,
            "error": "control websocket disconnected",
        }
    ]


def test_internal_open_is_loopback_only_and_response_is_sanitized() -> None:
    launcher = CapturingLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=iter(["launch-token-private"]).__next__),
        session_id_factory=iter(["local-session-private"]).__next__,
    )
    app = create_app(settings(), controller=controller)

    local = TestClient(app).post("/internal/open")
    remote = TestClient(app, client=("evil.example", 50000)).post("/internal/open")

    assert local.status_code == 200
    assert local.json() == {"status": "opened"}
    assert "token" not in local.text
    assert "session" not in local.text
    assert remote.status_code == 403
    assert launcher.urls and "activation=launch-token-private" in launcher.urls[0]
