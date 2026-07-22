from __future__ import annotations

import pytest

from realtime_action_spike.openai.interruption import (
    InterruptionEvent,
    InterruptionEventKind,
    InterruptionTimelineReducer,
)

SESSION_ID = "local-session-1234"


def event(
    kind: InterruptionEventKind,
    *,
    response_id: str = "resp-1",
    session_id: str = SESSION_ID,
    occurred_ns: int,
    user_speech_onset_ms: float | None = None,
) -> InterruptionEvent:
    return InterruptionEvent(
        local_session_id=session_id,
        response_id=response_id,
        kind=kind,
        occurred_ns=occurred_ns,
        user_speech_onset_ms=user_speech_onset_ms,
    )


def test_normal_interruption_order_produces_complete_trace() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=900, provider_audio_start_ms=240)

    assert reducer.consume(
        event(
            InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=1_000,
            user_speech_onset_ms=421.5,
        )
    )
    assert reducer.consume(
        event(InterruptionEventKind.PLAYBACK_SUPPRESSED, occurred_ns=11_001_000)
    )
    assert reducer.consume(
        event(InterruptionEventKind.RESPONSE_CANCELLED, occurred_ns=13_000_000)
    )
    assert reducer.consume(
        event(InterruptionEventKind.TRUNCATION_OBSERVED, occurred_ns=14_000_000)
    )
    assert reducer.consume(
        event(InterruptionEventKind.LISTENING_RESTORED, occurred_ns=20_000_000)
    )
    reducer.begin_response("resp-2", received_ns=30_000_000, provider_audio_start_ms=0)

    trace = reducer.traces[0]
    assert trace.response_id == "resp-1"
    assert trace.user_speech_onset_ms == 421.5
    assert trace.speech_started_received_ns == 1_000
    assert trace.provider_audio_start_ms == 240
    assert trace.playback_suppressed_ns == 11_001_000
    assert trace.response_cancelled_ns == 13_000_000
    assert trace.truncation_observed_ns == 14_000_000
    assert trace.listening_restored_ns == 20_000_000
    assert trace.next_response_first_audio_ns == 30_000_000
    assert trace.speech_start_to_audible_silence_ms == 11.0
    assert trace.complete is True


def test_browser_suppression_before_sideband_cancellation_is_preserved() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)

    reducer.consume(
        event(
            InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=1_000_000,
            user_speech_onset_ms=50.0,
        )
    )
    reducer.consume(
        event(InterruptionEventKind.PLAYBACK_SUPPRESSED, occurred_ns=2_000_000)
    )
    reducer.consume(
        event(InterruptionEventKind.RESPONSE_CANCELLED, occurred_ns=5_000_000)
    )

    trace = reducer.traces[0]
    assert trace.playback_suppressed_ns < trace.response_cancelled_ns
    assert trace.speech_start_to_audible_silence_ms == 1.0


def test_duplicate_events_use_first_observation_without_creating_extra_trace() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)

    first = event(
        InterruptionEventKind.SPEECH_STARTED,
        occurred_ns=1_000,
        user_speech_onset_ms=50.0,
    )
    duplicate = event(
        InterruptionEventKind.SPEECH_STARTED,
        occurred_ns=2_000,
        user_speech_onset_ms=80.0,
    )

    assert reducer.consume(first) is True
    assert reducer.consume(duplicate) is False
    assert len(reducer.traces) == 1
    assert reducer.traces[0].speech_started_received_ns == 1_000
    assert reducer.traces[0].user_speech_onset_ms == 50.0


def test_provider_truncation_is_optional_but_trace_can_be_complete() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)
    reducer.consume(
        event(
            InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=1_000,
            user_speech_onset_ms=50.0,
        )
    )
    reducer.consume(event(InterruptionEventKind.PLAYBACK_SUPPRESSED, occurred_ns=2_000))
    reducer.consume(event(InterruptionEventKind.RESPONSE_CANCELLED, occurred_ns=3_000))
    reducer.consume(event(InterruptionEventKind.LISTENING_RESTORED, occurred_ns=4_000))
    reducer.begin_response("resp-2", received_ns=5_000, provider_audio_start_ms=0)

    trace = reducer.traces[0]
    assert trace.truncation_observed_ns is None
    assert trace.complete is True


def test_stale_session_and_response_events_cannot_mutate_current_trace() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-current", received_ns=100, provider_audio_start_ms=25)

    assert (
        reducer.consume(
            event(
                InterruptionEventKind.SPEECH_STARTED,
                session_id="stale-session-1234",
                response_id="resp-current",
                occurred_ns=1_000,
            )
        )
        is False
    )
    assert (
        reducer.consume(
            event(
                InterruptionEventKind.SPEECH_STARTED,
                response_id="resp-stale",
                occurred_ns=1_000,
            )
        )
        is False
    )
    assert reducer.traces == ()


def test_late_event_for_previous_response_is_rejected_after_next_response() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)
    reducer.consume(event(InterruptionEventKind.SPEECH_STARTED, occurred_ns=1_000))
    reducer.begin_response("resp-2", received_ns=2_000, provider_audio_start_ms=0)

    assert (
        reducer.consume(
            event(
                InterruptionEventKind.RESPONSE_CANCELLED,
                response_id="resp-1",
                occurred_ns=3_000,
            )
        )
        is False
    )
    assert reducer.traces[0].response_cancelled_ns is None


def test_two_responses_create_two_separate_interruption_traces() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)
    reducer.consume(event(InterruptionEventKind.SPEECH_STARTED, occurred_ns=1_000))
    reducer.begin_response("resp-2", received_ns=2_000, provider_audio_start_ms=30)
    reducer.consume(
        event(
            InterruptionEventKind.SPEECH_STARTED,
            response_id="resp-2",
            occurred_ns=3_000,
        )
    )

    assert [trace.response_id for trace in reducer.traces] == ["resp-1", "resp-2"]
    assert reducer.traces[0].next_response_first_audio_ns == 2_000
    assert reducer.traces[1].next_response_first_audio_ns is None


def test_incomplete_trace_preserves_missing_fields_and_no_fabricated_duration() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=None)
    reducer.consume(event(InterruptionEventKind.SPEECH_STARTED, occurred_ns=1_000))

    trace = reducer.traces[0]
    assert trace.complete is False
    assert trace.provider_audio_start_ms is None
    assert trace.playback_suppressed_ns is None
    assert trace.speech_start_to_audible_silence_ms is None


@pytest.mark.parametrize("occurred_ns", [-1, True, 1.5])
def test_event_rejects_invalid_monotonic_timestamp(occurred_ns: object) -> None:
    with pytest.raises((TypeError, ValueError), match="occurred_ns"):
        InterruptionEvent(
            local_session_id=SESSION_ID,
            response_id="resp-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=occurred_ns,  # type: ignore[arg-type]
        )


def test_begin_response_rejects_reused_response_id() -> None:
    reducer = InterruptionTimelineReducer(SESSION_ID)
    reducer.begin_response("resp-1", received_ns=100, provider_audio_start_ms=25)

    with pytest.raises(ValueError, match="already observed"):
        reducer.begin_response("resp-1", received_ns=200, provider_audio_start_ms=25)
