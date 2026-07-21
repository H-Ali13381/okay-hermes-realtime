from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import pytest

from realtime_action_spike.runtime.timing import SessionTrace


def _steady_clock(values: list[int]) -> Iterator[int]:
    return iter(values)


def _wall_clock(values: list[str]) -> Iterator[str]:
    return iter(values)


def test_timing_records_preserve_event_order_and_source() -> None:
    monotonic = _steady_clock([100, 110, 120])
    wall = _wall_clock(["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"])

    trace = SessionTrace(
        session_id="timeline-session",
        monotonic_clock=lambda: next(monotonic),
        wall_clock=lambda: next(wall),
    )

    first = trace.record(
        "wake_detected",
        source="wake",
        data={"local_session_id": "timeline-session", "event_id": "evt-1"},
    )
    second = trace.record(
        "controller_offer_created",
        source="controller",
        data={"provider_call_id": "call-1", "sdp": "v=0\nraw-offer"},
    )
    third = trace.record(
        "browser_peer_connected",
        source="browser",
        data={"peer_connection_state": "connected", "data_channel_state": "open"},
    )

    assert [event.name for event in trace.events] == [
        "wake_detected",
        "controller_offer_created",
        "browser_peer_connected",
    ]
    assert [event.source for event in trace.events] == ["wake", "controller", "browser"]
    assert trace.events == [first, second, third]
    assert trace.events[0].monotonic_ns == 100


def test_record_rejects_backward_monotonic_time() -> None:
    monotonic = _steady_clock([1000, 900])
    wall = _wall_clock(["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"])

    trace = SessionTrace(
        session_id="timeline-session",
        monotonic_clock=lambda: next(monotonic),
        wall_clock=lambda: next(wall),
    )

    trace.record("start", source="controller", data={})

    with pytest.raises(ValueError, match="non-decreasing"):
        trace.record("backward", source="controller", data={})

    assert len(trace.events) == 1


def test_jsonl_serialization_excludes_secrets_and_raw_sdp_preserves_correlations() -> None:
    monotonic = _steady_clock([500, 600, 700])
    wall = _wall_clock(["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"])

    trace = SessionTrace(
        session_id="timeline-session",
        monotonic_clock=lambda: next(monotonic),
        wall_clock=lambda: next(wall),
    )

    trace.record(
        "function_call",
        source="controller",
        data={
            "local_session_id": "timeline-session",
            "openai_request_id": "openai-req-id",
            "provider_call_id": "provider-call-id",
            "event_id": "evt-id",
            "response": {"id": "response-id"},
            "item_id": "item-id",
            "peer_connection_state": "connecting",
            "data_channel_state": "connecting",
            "sdp": "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n",
            "arguments": {
                "api_key": "super-secret-key",
                "query": "play jazz",
            },
            "result": {
                "status": "ok",
                "authorization": "Bearer top-secret-token",
            },
            "execution_fingerprint": "a1b2c3d4",
        },
    )

    trace.record(
        "browser_state",
        source="browser",
        data={"peer_connection_state": "connected", "data_channel_state": "open"},
    )

    jsonl = trace.to_jsonl()
    lines = jsonl.splitlines()

    assert len(lines) == 2

    first = json.loads(lines[0])
    data = first["data"]

    assert data["local_session_id"] == "timeline-session"
    assert data["openai_request_id"] == "openai-req-id"
    assert data["provider_call_id"] == "provider-call-id"
    assert data["event_id"] == "evt-id"
    assert data["response"]["id"] == "response-id"
    assert data["item_id"] == "item-id"
    assert data["peer_connection_state"] == "connecting"
    assert data["data_channel_state"] == "connecting"
    assert "v=0\r\n" not in json.dumps(data)
    expected_sdp_hash = hashlib.sha256(
        b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"
    ).hexdigest()
    assert data["sdp_hash"] == expected_sdp_hash
    assert "super-secret-key" not in json.dumps(data)
    assert "top-secret-token" not in json.dumps(data)
    assert data["arguments"]["query"] == "play jazz"
    assert data["result"]["status"] == "ok"
    assert data["execution_fingerprint"] == "a1b2c3d4"
