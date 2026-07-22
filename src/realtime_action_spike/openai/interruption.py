from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from realtime_action_spike.runtime.timing import monotonic_delta_ms


class InterruptionEventKind(StrEnum):
    SPEECH_STARTED = "speech_started"
    PLAYBACK_SUPPRESSED = "playback_suppressed"
    RESPONSE_CANCELLED = "response_cancelled"
    TRUNCATION_OBSERVED = "truncation_observed"
    LISTENING_RESTORED = "listening_restored"


@dataclass(frozen=True, slots=True)
class InterruptionEvent:
    local_session_id: str
    response_id: str
    kind: InterruptionEventKind
    occurred_ns: int
    user_speech_onset_ms: float | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.local_session_id, "local_session_id", maximum=128)
        _validate_identifier(self.response_id, "response_id", maximum=256)
        if not isinstance(self.kind, InterruptionEventKind):
            raise TypeError("kind must be an InterruptionEventKind")
        _validate_ns(self.occurred_ns, "occurred_ns")
        if self.user_speech_onset_ms is not None:
            value = self.user_speech_onset_ms
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("user_speech_onset_ms must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError("user_speech_onset_ms must be finite and non-negative")
            if self.kind is not InterruptionEventKind.SPEECH_STARTED:
                raise ValueError("user_speech_onset_ms is only valid for speech_started")


@dataclass(slots=True)
class InterruptionTrace:
    response_id: str | None
    user_speech_onset_ms: float | None = None
    speech_started_received_ns: int | None = None
    provider_audio_start_ms: int | None = None
    playback_suppressed_ns: int | None = None
    response_cancelled_ns: int | None = None
    truncation_observed_ns: int | None = None
    listening_restored_ns: int | None = None
    next_response_first_audio_ns: int | None = None

    @property
    def speech_start_to_audible_silence_ms(self) -> float | None:
        return monotonic_delta_ms(
            self.speech_started_received_ns,
            self.playback_suppressed_ns,
        )

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.speech_started_received_ns,
                self.playback_suppressed_ns,
                self.response_cancelled_ns,
                self.listening_restored_ns,
                self.next_response_first_audio_ns,
            )
        )


class InterruptionTimelineReducer:
    """Reduce browser and OpenAI observations for one exact local session.

    The caller explicitly announces each response with ``begin_response``. Events
    are accepted only for the current response, so delayed observations cannot
    mutate a later response or session. Duplicate observations use first-write
    semantics to keep measured timestamps stable.
    """

    def __init__(self, local_session_id: str) -> None:
        _validate_identifier(local_session_id, "local_session_id", maximum=128)
        self._local_session_id = local_session_id
        self._active_response_id: str | None = None
        self._active_response_received_ns: int | None = None
        self._active_provider_audio_start_ms: int | None = None
        self._observed_response_ids: set[str] = set()
        self._traces: list[InterruptionTrace] = []
        self._traces_by_response: dict[str, InterruptionTrace] = {}

    @property
    def local_session_id(self) -> str:
        return self._local_session_id

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    @property
    def traces(self) -> tuple[InterruptionTrace, ...]:
        return tuple(self._traces)

    def begin_response(
        self,
        response_id: str,
        *,
        received_ns: int,
        provider_audio_start_ms: int | None,
    ) -> None:
        _validate_identifier(response_id, "response_id", maximum=256)
        _validate_ns(received_ns, "received_ns")
        if provider_audio_start_ms is not None:
            if isinstance(provider_audio_start_ms, bool) or not isinstance(
                provider_audio_start_ms, int
            ):
                raise TypeError("provider_audio_start_ms must be an integer")
            if provider_audio_start_ms < 0:
                raise ValueError("provider_audio_start_ms must be non-negative")
        if response_id in self._observed_response_ids:
            raise ValueError("response_id was already observed")

        previous = self._traces_by_response.get(self._active_response_id or "")
        if previous is not None and previous.next_response_first_audio_ns is None:
            previous.next_response_first_audio_ns = received_ns

        self._observed_response_ids.add(response_id)
        self._active_response_id = response_id
        self._active_response_received_ns = received_ns
        self._active_provider_audio_start_ms = provider_audio_start_ms

    def consume(self, event: InterruptionEvent) -> bool:
        if event.local_session_id != self._local_session_id:
            return False
        if event.response_id != self._active_response_id:
            return False
        if (
            self._active_response_received_ns is None
            or event.occurred_ns < self._active_response_received_ns
        ):
            return False

        trace = self._traces_by_response.get(event.response_id)
        if trace is None:
            trace = InterruptionTrace(
                response_id=event.response_id,
                provider_audio_start_ms=self._active_provider_audio_start_ms,
            )
            self._traces_by_response[event.response_id] = trace
            self._traces.append(trace)

        if event.kind is InterruptionEventKind.SPEECH_STARTED:
            if trace.speech_started_received_ns is not None:
                return False
            trace.speech_started_received_ns = event.occurred_ns
            trace.user_speech_onset_ms = (
                float(event.user_speech_onset_ms)
                if event.user_speech_onset_ms is not None
                else None
            )
            return True
        if event.kind is InterruptionEventKind.PLAYBACK_SUPPRESSED:
            return _set_first(trace, "playback_suppressed_ns", event.occurred_ns)
        if event.kind is InterruptionEventKind.RESPONSE_CANCELLED:
            return _set_first(trace, "response_cancelled_ns", event.occurred_ns)
        if event.kind is InterruptionEventKind.TRUNCATION_OBSERVED:
            return _set_first(trace, "truncation_observed_ns", event.occurred_ns)
        if event.kind is InterruptionEventKind.LISTENING_RESTORED:
            return _set_first(trace, "listening_restored_ns", event.occurred_ns)
        raise RuntimeError("unreachable interruption event kind")


def _set_first(trace: InterruptionTrace, field_name: str, value: int) -> bool:
    if getattr(trace, field_name) is not None:
        return False
    setattr(trace, field_name, value)
    return True


def _validate_identifier(value: str, name: str, *, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} length is out of range")
    if value != value.strip() or any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{name} contains invalid characters")


def _validate_ns(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
