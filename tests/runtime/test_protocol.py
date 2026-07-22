from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from realtime_action_spike.runtime.protocol import (
    ActionStateMessage,
    ActivationMessage,
    LoopbackMessage,
    PageReadyMessage,
    PageStartedMessage,
    RealtimeConnectedMessage,
    SessionClosedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    TimingMessage,
    TimingName,
    encode_loopback_message,
    parse_loopback_message,
)


def test_activation_round_trip_rounds_trip_via_encoder_and_parser() -> None:
    message = ActivationMessage(
        type="activation",
        probability=0.91,
        detected_at=1763731200.5,
        native_listener=True,
    )

    payload = encode_loopback_message(message)
    parsed = parse_loopback_message(payload)

    assert payload == json.dumps(
        message.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert parsed == message


def test_valid_session_bound_messages_round_trip() -> None:
    session_id = "local-session-01"

    messages: list[LoopbackMessage] = [
        PageReadyMessage(type="page_ready", session_id=session_id),
        PageStartedMessage(type="page_started", session_id=session_id),
        RealtimeConnectedMessage(
            type="realtime_connected",
            session_id=session_id,
            provider_call_id="call_provider_01",
        ),
        TimingMessage(
            type="timing",
            session_id=session_id,
            name=TimingName.PEER_CONNECTION_STATE,
            monotonic_ms=42.0,
            data={"state": "connected"},
        ),
        StopMessage(type="stop", session_id=session_id, reason=StopReason.BUTTON),
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id),
        SessionClosedMessage(
            type="session_closed",
            session_id=session_id,
            outcome=SessionOutcome.COMPLETED,
        ),
        ActionStateMessage(
            type="action_state",
            session_id=session_id,
            capability="assistant_get_current_time",
            state="completed",
            message="Current time retrieved",
        ),
    ]

    for message in messages:
        assert parse_loopback_message(encode_loopback_message(message)) == message


@pytest.mark.parametrize(
    "provider_call_id",
    ["", "call id", "https://api.openai.com/v1/realtime/calls/call_01", "call_01\nsecret"],
)
def test_realtime_connected_rejects_malformed_provider_call_id(
    provider_call_id: str,
) -> None:
    with pytest.raises(ValidationError):
        parse_loopback_message(
            {
                "type": "realtime_connected",
                "session_id": "local-session-01",
                "provider_call_id": provider_call_id,
            }
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"call_id": "call_provider_secret"},
        {"arguments": {"timezone": "UTC"}},
        {"api_key": "secret"},
        {"result": {"iso_time": "2026-07-21T22:00:00Z"}},
    ],
)
def test_action_state_rejects_provider_and_execution_material(extra: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "type": "action_state",
        "session_id": "local-session-01",
        "capability": "assistant_get_current_time",
        "state": "completed",
        **extra,
    }

    with pytest.raises(ValidationError):
        parse_loopback_message(payload)


def test_parse_accepts_bytes_and_string_payloads() -> None:
    message = ActivationMessage(
        type="activation",
        probability=0.25,
        detected_at=1763731200.5,
        native_listener=False,
    )
    encoded = encode_loopback_message(message)

    assert parse_loopback_message(encoded) == message
    assert parse_loopback_message(encoded.encode()) == message


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "activation", "detected_at": 1.0, "native_listener": True},
        {"type": "page_ready"},
        {
            "type": "timing",
            "session_id": "local-session-01",
            "name": TimingName.PEER_CONNECTION_STATE.value,
            "monotonic_ms": 3.0,
        },
    ],
)
def test_missing_required_fields_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "activation",
            "probability": 0.3,
            "detected_at": 1.0,
            "native_listener": True,
            "unexpected": 1,
        },
        {
            "type": "timing",
            "session_id": "local-session-01",
            "name": TimingName.DATA_CHANNEL_STATE.value,
            "monotonic_ms": 1.0,
            "data": {"state": "open", "unexpected": True},
        },
    ],
)
def test_extra_fields_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(payload)


def test_strict_no_coercion_for_probability_detected_at_and_native_listener() -> None:
    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(
            {
                "type": "activation",
                "probability": 1,
                "detected_at": 1.0,
                "native_listener": True,
            }
        )

    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(
            {
                "type": "activation",
                "probability": "0.5",
                "detected_at": 1.0,
                "native_listener": True,
            }
        )

    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(
            {
                "type": "activation",
                "probability": 0.5,
                "detected_at": 1,
                "native_listener": True,
            }
        )

    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(
            {
                "type": "activation",
                "probability": 0.5,
                "detected_at": 1.0,
                "native_listener": "True",
            }
        )

    with pytest.raises((ValidationError, TypeError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": "local-session-01",
                "name": TimingName.PLAYBACK_SUPPRESSED.value,
                "monotonic_ms": 1,
                "data": {"suppressed": True},
            }
        )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf")],
)
def test_probability_rejects_non_finite_values(value: float) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "activation",
                "probability": value,
                "detected_at": 1.0,
                "native_listener": True,
            }
        )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf")],
)
def test_timing_monotonic_ms_rejects_non_finite_values(value: float) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": "local-session-01",
                "name": TimingName.PLAYBACK_SUPPRESSED.value,
                "monotonic_ms": value,
                "data": {},
            }
        )


def test_timing_monotonic_ms_rejects_negative_values() -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": "local-session-01",
                "name": TimingName.PLAYBACK_SUPPRESSED.value,
                "monotonic_ms": -0.1,
                "data": {},
            }
        )


def test_invalid_stop_reason_outcome_or_timing_name_rejected() -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "stop",
                "session_id": "local-session-01",
                "reason": "not-a-reason",
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "session_closed",
                "session_id": "local-session-01",
                "outcome": "not-an-outcome",
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": "local-session-01",
                "name": "not-a-name",
                "monotonic_ms": 1.0,
            }
        )


@pytest.mark.parametrize(
    "session_id",
    [
        "short",
        "call_" + ("x" * 8),
        "session_" + ("x" * 8),
        "a-bad@session-id",
        "no space",
    ],
)
def test_malformed_session_ids_rejected(session_id: str) -> None:
    with pytest.raises((ValidationError, ValueError, TypeError)):
        parse_loopback_message(
            {
                "type": "page_ready",
                "session_id": session_id,
            }
        )


def test_timing_name_keys_are_narrow_and_required() -> None:
    base_session_id = "local-session-01"

    valid_playback = parse_loopback_message(
        {
            "type": "timing",
            "session_id": base_session_id,
            "name": TimingName.PLAYBACK_SUPPRESSED.value,
            "monotonic_ms": 10.0,
            "data": {},
        }
    )
    assert isinstance(valid_playback, TimingMessage)

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": base_session_id,
                "name": TimingName.PLAYBACK_SUPPRESSED.value,
                "monotonic_ms": 10.0,
                "data": {"suppressed": "yes"},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": base_session_id,
                "name": TimingName.PEER_CONNECTION_STATE.value,
                "monotonic_ms": 10.0,
                "data": {},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": base_session_id,
                "name": TimingName.PEER_CONNECTION_STATE.value,
                "monotonic_ms": 10.0,
                "data": {"state": "bad"},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": base_session_id,
                "name": TimingName.DATA_CHANNEL_STATE.value,
                "monotonic_ms": 10.0,
                "data": {"state": "bad"},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": base_session_id,
                "name": TimingName.TRANSPORT_FAILURE.value,
                "monotonic_ms": 10.0,
                "data": {"state": "bad"},
            }
        )


def test_realtime_diagnostic_timings_are_bounded_and_narrow() -> None:
    session_id = "local-session-01"
    response = parse_loopback_message(
        {
            "type": "timing",
            "session_id": session_id,
            "name": "realtime_response_done",
            "monotonic_ms": 10.0,
            "data": {
                "response_id": "resp-123",
                "status": "failed",
                "output_types": ["message"],
                "suppressed_response_id": "resp-122",
                "pending_restore_response_id": "resp-123",
                "remote_audio_muted": True,
            },
        }
    )
    assert isinstance(response, TimingMessage)

    error = parse_loopback_message(
        {
            "type": "timing",
            "session_id": session_id,
            "name": "realtime_error",
            "monotonic_ms": 11.0,
            "data": {
                "error_type": "invalid_request_error",
                "code": "unsupported_tool",
                "message": "No matching media tool was supplied",
            },
        }
    )
    assert isinstance(error, TimingMessage)

    for invalid_data in (
        {"error_type": "provider", "message": "x" * 513},
        {"error_type": "provider", "raw_event": {}},
        {
            "response_id": "resp-123",
            "status": "unknown",
            "output_types": [],
            "remote_audio_muted": False,
        },
    ):
        with pytest.raises((ValidationError, ValueError)):
            parse_loopback_message(
                {
                    "type": "timing",
                    "session_id": session_id,
                    "name": (
                        "realtime_response_done"
                        if "response_id" in invalid_data
                        else "realtime_error"
                    ),
                    "monotonic_ms": 12.0,
                    "data": invalid_data,
                }
            )


def test_sdp_offer_and_answer_timing_only_accept_sha256_hashes() -> None:
    session_id = "local-session-01"
    valid_hash = "ab" * 32

    parse_loopback_message(
        {
            "type": "timing",
            "session_id": session_id,
            "name": TimingName.SDP_OFFER_CREATED.value,
            "monotonic_ms": 10.0,
            "data": {"sdp_hash": valid_hash},
        }
    )

    parse_loopback_message(
        {
            "type": "timing",
            "session_id": session_id,
            "name": TimingName.SDP_ANSWER_APPLIED.value,
            "monotonic_ms": 10.0,
            "data": {},
        }
    )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": session_id,
                "name": TimingName.SDP_OFFER_CREATED.value,
                "monotonic_ms": 10.0,
                "data": {"sdp": "v=0"},
            }
        )

    with pytest.raises((ValidationError, ValueError)):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": session_id,
                "name": TimingName.SDP_ANSWER_APPLIED.value,
                "monotonic_ms": 10.0,
                "data": {"sdp_hash": "ABC"},
            }
        )


def test_secret_like_keys_rejected_any_nesting_depth() -> None:
    session_id = "local-session-01"

    nested_payloads = [
        {"state": "connected", "metadata": {"api_key": "leaky"}},
        {"state": "connected", "metadata": {"password": "secret"}},
        {"state": "connected", "metadata": {"auth": {"authorization": "bearer ..."}}},
        {
            "state": "connected",
            "metadata": {"nested": {"inner": {"call_id": "call_xx"}}},
        },
        {
            "state": "connected",
            "metadata": {"inner": {"details": {"sideband_url": "https://example"}}},
        },
    ]

    for payload in nested_payloads:
        with pytest.raises((ValidationError, ValueError)):
            parse_loopback_message(
                {
                    "type": "timing",
                    "session_id": session_id,
                    "name": TimingName.PEER_CONNECTION_STATE.value,
                    "monotonic_ms": 10.0,
                    "data": payload,
                }
            )


def test_unknown_type_is_rejected() -> None:
    with pytest.raises((ValidationError, ValueError, TypeError)):
        parse_loopback_message(
            {
                "type": "not_a_type",
                "session_id": "local-session-01",
                "monotonic_ms": 0.0,
                "name": TimingName.PEER_CONNECTION_STATE.value,
                "reason": "button",
            }
        )


def test_expected_session_id_mismatch_is_rejected_for_session_bound_messages() -> None:
    message = {
        "type": "page_started",
        "session_id": "local-session-01",
    }

    parsed = parse_loopback_message(message, expected_session_id="local-session-01")
    assert isinstance(parsed, PageStartedMessage)
    assert parsed.session_id == message["session_id"]

    with pytest.raises((ValueError, ValidationError)):
        parse_loopback_message(message, expected_session_id="other-session")


def test_expected_session_id_is_ignored_for_activation_messages() -> None:
    activation_message = {
        "type": "activation",
        "probability": 0.8,
        "detected_at": 12.5,
        "native_listener": True,
    }

    parsed = parse_loopback_message(activation_message, expected_session_id="different")
    assert isinstance(parsed, ActivationMessage)
    assert parsed.probability == activation_message["probability"]
    assert parsed.detected_at == activation_message["detected_at"]
    assert parsed.native_listener == activation_message["native_listener"]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (
            TimingName.NEXT_RESPONSE_FIRST_AUDIO,
            {"response_id": "resp-next", "interrupted_response_id": "resp-old"},
        ),
        (
            TimingName.LISTENING_RESTORED,
            {"response_id": "resp-next", "interrupted_response_id": "resp-old"},
        ),
    ],
)
def test_interruption_timing_markers_require_explicit_response_ids(
    name: TimingName,
    data: dict[str, str],
) -> None:
    parsed = parse_loopback_message(
        {
            "type": "timing",
            "session_id": "local-session-01",
            "name": name.value,
            "monotonic_ms": 10.0,
            "data": data,
        }
    )
    assert isinstance(parsed, TimingMessage)
    assert parsed.data == data

    with pytest.raises(ValueError, match="response_id"):
        parse_loopback_message(
            {
                "type": "timing",
                "session_id": "local-session-01",
                "name": name.value,
                "monotonic_ms": 10.0,
                "data": {},
            }
        )


def test_playback_suppressed_accepts_scoped_response_id() -> None:
    parsed = parse_loopback_message(
        {
            "type": "timing",
            "session_id": "local-session-01",
            "name": TimingName.PLAYBACK_SUPPRESSED.value,
            "monotonic_ms": 10.0,
            "data": {"suppressed": True, "response_id": "resp-current"},
        }
    )

    assert isinstance(parsed, TimingMessage)
    assert parsed.data["response_id"] == "resp-current"
