from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]

type TimingEventSource = Literal["wake", "controller", "browser", "openai"]


@dataclass(frozen=True, slots=True)
class TimingEvent:
    name: str
    monotonic_ns: int
    wall_time: str
    source: TimingEventSource
    data: dict[str, JsonValue]


_CORRELATION_KEYS: frozenset[str] = frozenset(
    {
        "local_session_id",
        "openai_request_id",
        "provider_call_id",
        "event_id",
        "item_id",
        "peer_connection_state",
        "data_channel_state",
        "response",
        "execution_fingerprint",
        "id",
    }
)

_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "authorization",
    "bearer ",
    "token",
    "password",
    "secret",
)

_SDP_KEYS: frozenset[str] = frozenset({"sdp", "offer_sdp", "answer_sdp", "local_sdp", "remote_sdp"})


def _default_wall_clock() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SECRET_SUBSTRINGS)


def _sanitize_scalar(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _sanitize_dict(value)
    if isinstance(value, list):
        return [_sanitize_scalar(item) for item in value]
    return value


def _sanitize_dict(value: dict[str, JsonValue], *, is_function_payload: bool = False) -> JsonValue:
    output: dict[str, JsonValue] = {}
    for key, item in value.items():
        lowered_key = key.lower()

        if lowered_key in _SDP_KEYS and isinstance(item, str):
            output["sdp_hash"] = _sha256_hex(item)
            continue

        if is_function_payload and _is_secret_key(lowered_key):
            continue

        if _is_secret_key(key) and not (is_function_payload or lowered_key in _CORRELATION_KEYS):
            output[key] = "[REDACTED]"
            continue

        output[key] = _sanitize_scalar(item)
    return output


def _sanitize_arguments_or_result(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _sanitize_dict(value, is_function_payload=True)
    if isinstance(value, list):
        return [_sanitize_arguments_or_result(item) for item in value]
    return value


def sanitize_timing_data(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Sanitize timing payload for durable telemetry output."""

    sanitized: dict[str, JsonValue] = {}
    for key, value in data.items():
        lowered_key = key.lower()

        if lowered_key in _SDP_KEYS and isinstance(value, str):
            sanitized["sdp_hash"] = _sha256_hex(value)
            continue

        if key in ("arguments", "result"):
            sanitized[key] = _sanitize_arguments_or_result(value)
            continue

        sanitized[key] = _sanitize_scalar(value)

    return sanitized


@dataclass(slots=True)
class SessionTrace:
    session_id: str
    events: list[TimingEvent] = field(default_factory=list)
    monotonic_clock: Callable[[], int] = lambda: time.monotonic_ns()
    wall_clock: Callable[[], str] = _default_wall_clock

    def record(
        self,
        name: str,
        *,
        source: TimingEventSource,
        data: dict[str, JsonValue] | None = None,
        monotonic_ns: int | None = None,
        wall_time: str | None = None,
    ) -> TimingEvent:
        """Record an event, enforcing monotonic ordering.

        The optional ``monotonic_ns`` and ``wall_time`` fields are primarily for tests.
        """

        monotonic_value = monotonic_ns if monotonic_ns is not None else self.monotonic_clock()
        wall_time_value = wall_time if wall_time is not None else self.wall_clock()

        if self.events and monotonic_value < self.events[-1].monotonic_ns:
            raise ValueError("timing record monotonic_ns must be non-decreasing")

        payload = data or {}
        event = TimingEvent(
            name=name,
            monotonic_ns=monotonic_value,
            wall_time=wall_time_value,
            source=source,
            data=dict(payload),
        )
        self.events.append(event)
        return event

    def _serialize_event(self, event: TimingEvent) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "name": event.name,
            "monotonic_ns": event.monotonic_ns,
            "wall_time": event.wall_time,
            "source": event.source,
            "data": sanitize_timing_data(event.data),
        }

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(self._serialize_event(event), separators=(",", ":"))
            for event in self.events
        )

    def to_jsonl_lines(self) -> list[str]:
        return [
            json.dumps(self._serialize_event(event), separators=(",", ":"))
            for event in self.events
        ]

    def write_jsonl(self, path: str | Path) -> None:
        path = Path(path)
        path.write_text(self.to_jsonl(), encoding="utf-8")


def _load_json_lines(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line]
