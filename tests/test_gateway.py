from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from realtime_action_spike.config import Settings, build_realtime_session
from realtime_action_spike.gateway import OPENAI_REALTIME_CALLS_URL, create_app


class StubUpstreamClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


def settings(api_key: str | None = "test-secret-key") -> Settings:
    return Settings(openai_api_key=api_key)


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
        headers={"Content-Type": "application/sdp"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.headers["x-openai-request-id"] == "req_realtime_test"
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
    client = TestClient(create_app(settings()))

    response = client.post(
        "/execute",
        json={
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


def test_execute_endpoint_rejects_unknown_capability_as_structured_error() -> None:
    client = TestClient(create_app(settings()))

    response = client.post(
        "/execute",
        json={"call_id": "call_bad", "name": "run_shell", "arguments": {}},
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
