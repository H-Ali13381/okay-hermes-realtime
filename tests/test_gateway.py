from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi.testclient import TestClient

import realtime_action_spike.gateway as gateway_module
from realtime_action_spike.capabilities import CapabilityBroker
from realtime_action_spike.config import Settings, build_realtime_session
from realtime_action_spike.gateway import OPENAI_REALTIME_CALLS_URL, create_app


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
        return httpx.Response(201, text=f"v=0\r\nanswer-{call_number}")


class CountingBroker(CapabilityBroker):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | dict[str, Any]]] = []

    def execute(self, name: str, arguments: str | Mapping[str, Any]) -> dict[str, Any]:
        recorded_arguments = arguments if isinstance(arguments, str) else dict(arguments)
        self.calls.append((name, recorded_arguments))
        return {
            "ok": True,
            "capability": name,
            "execution": "local",
            "result": {"execution_count": len(self.calls)},
        }


class MutableResultBroker(CapabilityBroker):
    def __init__(self) -> None:
        self.execution_count = 0
        self.result = {"status": "original"}

    def execute(self, name: str, arguments: str | Mapping[str, Any]) -> dict[str, Any]:
        self.execution_count += 1
        return {
            "ok": True,
            "capability": name,
            "execution": "local",
            "result": self.result,
        }


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


def started_execution_client(
    broker: CapabilityBroker | None = None,
) -> tuple[TestClient, str]:
    upstream = StubUpstreamClient(httpx.Response(201, text="v=0\r\nmock-answer"))
    client = TestClient(create_app(settings(), upstream_client=upstream, broker=broker))
    return client, start_openai_realtime_session(client)


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
    assert session["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-mini-transcribe"
    }
    assert session["tool_choice"] == "auto"
    assert len(session["tools"]) == 6
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
            "assistant_start_timer",
            "media_play",
            "media_control",
            "voice_end_session",
            "agent_delegate_task",
        ],
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


def test_session_endpoint_rejects_wrong_media_type() -> None:
    client = TestClient(create_app(settings()))

    response = client.post("/session", content="v=0\r\nmock-offer")

    assert response.status_code == 415


def test_session_endpoint_relays_sdp_and_server_owned_configuration() -> None:
    upstream = StubUpstreamClient(
        httpx.Response(
            201,
            text="v=0\r\nmock-answer",
            headers={"x-request-id": "req_realtime_test"},
        )
    )
    client = TestClient(create_app(settings(), upstream_client=upstream))

    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={
            "Content-Type": "application/sdp",
            "Origin": "http://127.0.0.1:8501",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["x-openai-request-id"] == "req_realtime_test"
    assert response.headers["x-openai-realtime-session-id"]
    assert "X-OpenAI-Realtime-Session-ID" in response.headers["access-control-expose-headers"]
    assert response.text == "v=0\r\nmock-answer"
    assert len(upstream.calls) == 1

    call = upstream.calls[0]
    assert call["url"] == OPENAI_REALTIME_CALLS_URL
    assert call["headers"]["Authorization"] == "Bearer test-secret-key"
    assert call["headers"]["OpenAI-Safety-Identifier"]
    assert call["files"]["sdp"] == (None, "v=0\r\nmock-offer", "application/sdp")
    session_json = call["files"]["session"][1]
    assert json.loads(session_json)["model"] == "gpt-realtime-2.1-mini"


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
        "request_id": "req_failed_test",
    }
    assert "test-secret-key" not in response.text


def test_execute_endpoint_returns_allowlisted_result_with_call_id() -> None:
    client, session_id = started_execution_client()

    response = client.post(
        "/execute",
        json={
            "session_id": session_id,
            "call_id": "call_123",
            "name": "media_play",
            "arguments": {"query": "Daft Punk"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["call_id"] == "call_123"
    assert body["ok"] is True
    assert body["capability"] == "media_play"
    assert body["result"]["action"] == "media.play"


def test_execute_endpoint_replays_same_openai_call_id_without_reexecuting() -> None:
    broker = CountingBroker()
    client, session_id = started_execution_client(broker)
    request = {
        "session_id": session_id,
        "call_id": "call_idempotent",
        "name": "media_play",
        "arguments": {"query": "Daft Punk", "media_type": "music"},
    }

    first = client.post("/execute", json=request)
    second = client.post("/execute", json=request)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert broker.calls == [
        ("media_play", {"query": "Daft Punk", "media_type": "music"})
    ]


def test_execute_replay_uses_immutable_serialized_result() -> None:
    broker = MutableResultBroker()
    client, session_id = started_execution_client(broker)
    request = {
        "session_id": session_id,
        "call_id": "call_immutable",
        "name": "media_control",
        "arguments": {"action": "pause"},
    }

    first = client.post("/execute", json=request)
    broker.result["status"] = "mutated-after-response"
    replay = client.post("/execute", json=request)

    assert first.json()["result"] == {"status": "original"}
    assert replay.json() == first.json()
    assert broker.execution_count == 1


def test_execute_fingerprint_normalizes_equivalent_json_argument_encodings() -> None:
    broker = CountingBroker()
    client, session_id = started_execution_client(broker)
    request: dict[str, Any] = {
        "session_id": session_id,
        "call_id": "call_equivalent_json",
        "name": "media_play",
        "arguments": '{"media_type":"music","query":"Daft Punk"}',
    }

    first = client.post("/execute", json=request)
    request["arguments"] = {"query": "Daft Punk", "media_type": "music"}
    replay = client.post("/execute", json=request)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(broker.calls) == 1


def test_execute_endpoint_rejects_changed_payload_for_reused_openai_call_id() -> None:
    broker = CountingBroker()
    client, session_id = started_execution_client(broker)

    first = client.post(
        "/execute",
        json={
            "session_id": session_id,
            "call_id": "call_conflict",
            "name": "media_control",
            "arguments": {"action": "pause"},
        },
    )
    conflict = client.post(
        "/execute",
        json={
            "session_id": session_id,
            "call_id": "call_conflict",
            "name": "media_control",
            "arguments": {"action": "next"},
        },
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json() == {
        "ok": False,
        "call_id": "call_conflict",
        "error": {
            "type": "call_id_conflict",
            "message": "OpenAI call_id was already used with a different request",
        },
    }
    assert broker.calls == [("media_control", {"action": "pause"})]


def test_successful_realtime_session_resets_openai_call_id_scope() -> None:
    broker = CountingBroker()
    upstream = StubUpstreamClient(httpx.Response(201, text="v=0\r\nmock-answer"))
    client = TestClient(create_app(settings(), upstream_client=upstream, broker=broker))
    first_session_id = start_openai_realtime_session(client, "first-offer")
    execution = {
        "session_id": first_session_id,
        "call_id": "call_scoped_to_session",
        "name": "media_control",
        "arguments": {"action": "pause"},
    }

    before_new_session = client.post("/execute", json=execution)
    second_session_id = start_openai_realtime_session(client, "new-offer")
    stale_session = client.post("/execute", json=execution)
    execution["session_id"] = second_session_id
    after_new_session = client.post("/execute", json=execution)

    assert before_new_session.status_code == 200
    assert second_session_id != first_session_id
    assert stale_session.status_code == 409
    assert stale_session.json()["error"]["type"] == "stale_session"
    assert after_new_session.status_code == 200
    assert len(broker.calls) == 2
    assert after_new_session.json()["result"]["execution_count"] == 2


def test_failed_session_creation_preserves_current_execution_scope() -> None:
    broker = CountingBroker()
    upstream = StubUpstreamClient(httpx.Response(201, text="v=0\r\nmock-answer"))
    client = TestClient(create_app(settings(), upstream_client=upstream, broker=broker))
    session_id = start_openai_realtime_session(client)
    execution = {
        "session_id": session_id,
        "call_id": "call_survives_failed_session",
        "name": "media_control",
        "arguments": {"action": "pause"},
    }

    first = client.post("/execute", json=execution)
    upstream.response = httpx.Response(401, text='{"error":{"message":"denied"}}')
    failed_session = client.post(
        "/session",
        content="v=0\r\nfailed-offer",
        headers={"Content-Type": "application/sdp"},
    )
    replay = client.post("/execute", json=execution)

    assert first.status_code == 200
    assert failed_session.status_code == 502
    assert replay.json() == first.json()
    assert len(broker.calls) == 1


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

            current_session_id = second.headers["x-openai-realtime-session-id"]
            execution = await client.post(
                "/execute",
                json={
                    "session_id": current_session_id,
                    "call_id": "call_after_race",
                    "name": "media_control",
                    "arguments": {"action": "pause"},
                },
            )
            assert execution.status_code == 200

    asyncio.run(run_scenario())


def test_execute_requires_the_current_openai_realtime_session_scope() -> None:
    broker = CountingBroker()
    upstream = StubUpstreamClient(httpx.Response(201, text="v=0\r\nmock-answer"))
    client = TestClient(create_app(settings(), upstream_client=upstream, broker=broker))
    request = {
        "session_id": "stale-session",
        "call_id": "call_scoped",
        "name": "media_control",
        "arguments": {"action": "pause"},
    }

    without_session = client.post("/execute", json=request)
    session = client.post(
        "/session",
        content="v=0\r\nfirst-offer",
        headers={"Content-Type": "application/sdp"},
    )
    session_id = session.headers.get("x-openai-realtime-session-id")
    request["session_id"] = session_id
    current = client.post("/execute", json=request)

    assert without_session.status_code == 409
    assert session_id
    assert current.status_code == 200
    assert len(broker.calls) == 1


def test_openai_realtime_session_rejects_new_calls_at_safety_limit(monkeypatch) -> None:
    monkeypatch.setattr(gateway_module, "MAX_OPENAI_CALLS_PER_SESSION", 2, raising=False)
    broker = CountingBroker()
    upstream = StubUpstreamClient(httpx.Response(201, text="v=0\r\nmock-answer"))
    client = TestClient(create_app(settings(), upstream_client=upstream, broker=broker))
    session = client.post(
        "/session",
        content="v=0\r\nlimited-offer",
        headers={"Content-Type": "application/sdp"},
    )
    session_id = session.headers.get("x-openai-realtime-session-id")

    responses = [
        client.post(
            "/execute",
            json={
                "session_id": session_id,
                "call_id": f"call_{index}",
                "name": "media_control",
                "arguments": {"action": "pause"},
            },
        )
        for index in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].json()["error"]["type"] == "session_call_limit_exceeded"
    assert len(broker.calls) == 2


def test_execute_endpoint_rejects_unknown_capability_as_structured_error() -> None:
    client, session_id = started_execution_client()

    response = client.post(
        "/execute",
        json={
            "session_id": session_id,
            "call_id": "call_bad",
            "name": "run_shell",
            "arguments": {},
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "call_id": "call_bad",
        "error": {
            "type": "unknown_capability",
            "message": "unknown capability: run_shell",
        },
    }


def test_cors_allows_only_loopback_streamlit_origin() -> None:
    client = TestClient(create_app(settings()))

    allowed = client.options(
        "/execute",
        headers={
            "Origin": "http://127.0.0.1:8501",
            "Access-Control-Request-Method": "POST",
        },
    )
    blocked = client.options(
        "/execute",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8501"
    assert "access-control-allow-origin" not in blocked.headers
