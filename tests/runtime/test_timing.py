from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from realtime_action_spike.runtime.timing import SessionTrace, monotonic_delta_ms


def _steady_clock(values: list[int]) -> Iterator[int]:
    return iter(values)


def _wall_clock(values: list[str]) -> Iterator[str]:
    return iter(values)


def test_monotonic_delta_ms_preserves_missing_and_reversed_markers() -> None:
    assert monotonic_delta_ms(1_000_000, 12_000_000) == 11.0
    assert monotonic_delta_ms(None, 12_000_000) is None
    assert monotonic_delta_ms(1_000_000, None) is None
    assert monotonic_delta_ms(12_000_000, 1_000_000) is None


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_monotonic_delta_ms_rejects_invalid_markers(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="start_ns"):
        monotonic_delta_ms(value, 2_000_000)  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    "raw_value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_record_rejects_non_finite_json_numbers(raw_value: float) -> None:
    trace = SessionTrace(session_id="timeline-session")

    with pytest.raises(ValueError, match=r"finite JSON numbers"):
        trace.record("function_call", source="controller", data={"metric": raw_value})


def test_record_accepts_finite_numbers() -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record("function_call", source="controller", data={"metric": 42.5, "ratio": 0.0})

    data = json.loads(trace.to_jsonl())["data"]

    assert data["metric"] == 42.5
    assert data["ratio"] == 0.0


def test_jsonl_redacts_bearer_credentials_in_nested_string_values_case_insensitively() -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record(
        "function_call",
        source="controller",
        data={
            "local_session_id": "timeline-session",
            "payload": {
                "raw": "Bearer live-secret",
                "notes": ["ignore this", "bEaReR another-secret"],
            },
            "bearer": "top-secret-token",
            "notes": "the bearer of light",
        },
    )

    data = json.loads(trace.to_jsonl())["data"]

    assert data["local_session_id"] == "timeline-session"
    assert data["payload"]["raw"] == "[REDACTED]"
    assert data["payload"]["notes"] == ["ignore this", "[REDACTED]"]
    assert data["bearer"] == "[REDACTED]"
    assert data["notes"] == "the bearer of light"
    assert "live-secret" not in json.dumps(data)


@pytest.mark.parametrize(
    "secret_key",
    ["client_secret_value", "openai_api_key_backup", "authorization_header"],
)
def test_jsonl_redacts_compound_nested_secret_keys(secret_key: str) -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record(
        "function_call",
        source="controller",
        data={
            "local_session_id": "timeline-session",
            "metadata": {
                secret_key: "must-not-leak",
            },
        },
    )

    data = json.loads(trace.to_jsonl())["data"]

    assert data["metadata"][secret_key] == "[REDACTED]"
    assert "must-not-leak" not in json.dumps(data)


def test_safe_nested_control_fields_are_not_redacted() -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record(
        "function_call",
        source="controller",
        data={
            "metadata": {
                "token_count": 4,
                "secretary_note": "approved",
            },
        },
    )

    data = json.loads(trace.to_jsonl())["data"]

    assert data["metadata"]["token_count"] == 4
    assert data["metadata"]["secretary_note"] == "approved"


def test_record_copies_data_deeply_to_prevent_nested_mutation_leakage() -> None:
    event_payload = {"nested": {"tokens": ["Bearer live-secret", "keep"]}}
    trace = SessionTrace(session_id="timeline-session")

    trace.record("function_call", source="controller", data=event_payload)
    event_payload["nested"]["tokens"][0] = "changed"

    data = json.loads(trace.to_jsonl())["data"]

    assert data["nested"] == {"tokens": ["[REDACTED]", "keep"]}


@pytest.mark.parametrize(
    "runtime_value",
    [
        {"tuple": (1, 2, 3)},
        {"set": {"a", "b"}},
        {"bytes": b"raw-bytes"},
        {"custom": object()},
    ],
)
def test_record_rejects_non_json_runtime_values(runtime_value: dict[str, object]) -> None:
    trace = SessionTrace(session_id="timeline-session")

    with pytest.raises((TypeError, ValueError), match=r"unsupported|not JSON|json"):
        trace.record("bad_payload", source="controller", data=runtime_value)


def test_record_accepts_recursive_json_values() -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record(
        "json_payload",
        source="controller",
        data={
            "level": {
                "name": "ok",
                "flags": [True, False, None],
                "counts": [1, 2, 3],
            }
        },
    )

    data = json.loads(trace.to_jsonl())["data"]

    assert data["level"]["name"] == "ok"
    assert data["level"]["flags"] == [True, False, None]


def test_record_generates_distinct_sdp_hash_fields_for_each_variant() -> None:
    trace = SessionTrace(session_id="timeline-session")

    trace.record(
        "sdp_fields",
        source="controller",
        data={
            "sdp": "v=0\no=sdp",
            "offer_sdp": "v=0\no=offer",
            "answer_sdp": "v=0\no=answer",
            "local_sdp": "v=0\no=local",
            "remote_sdp": "v=0\no=remote",
        },
    )

    data = json.loads(trace.to_jsonl())["data"]

    assert data == {
        "sdp_hash": hashlib.sha256(b"v=0\no=sdp").hexdigest(),
        "offer_sdp_hash": hashlib.sha256(b"v=0\no=offer").hexdigest(),
        "answer_sdp_hash": hashlib.sha256(b"v=0\no=answer").hexdigest(),
        "local_sdp_hash": hashlib.sha256(b"v=0\no=local").hexdigest(),
        "remote_sdp_hash": hashlib.sha256(b"v=0\no=remote").hexdigest(),
    }


def test_write_jsonl_creates_parent_directories(tmp_path: Path) -> None:
    trace = SessionTrace(session_id="timeline-session")
    trace.record("wake_detected", source="wake", data={"openai_request_id": "req-id"})

    output = tmp_path / "levels" / "nested" / "events.jsonl"

    trace.write_jsonl(output)

    assert output.exists()
    assert output.parent.exists()

    loaded = json.loads(trace.to_jsonl_lines()[0])
    assert loaded["session_id"] == "timeline-session"


def test_write_jsonl_atomically_replaces_an_existing_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace = SessionTrace(session_id="atomic-session")
    trace.record("session_closed", source="controller", data={"outcome": "completed"})
    output = tmp_path / "events.jsonl"
    output.write_text("previous-complete-trace", encoding="utf-8")
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def observe_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert output.read_text(encoding="utf-8") == "previous-complete-trace"
        assert source_path.read_text(encoding="utf-8") == trace.to_jsonl()
        replacements.append((source_path, destination_path))
        real_replace(source_path, destination_path)

    monkeypatch.setattr("realtime_action_spike.runtime.timing.os.replace", observe_replace)

    trace.write_jsonl(output)

    assert len(replacements) == 1
    assert replacements[0][1] == output
    assert output.read_text(encoding="utf-8") == trace.to_jsonl()
    assert not list(tmp_path.glob(".events.jsonl.*.tmp"))
