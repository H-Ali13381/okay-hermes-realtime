from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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

_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_SECRET_KEY_SHAPES: tuple[tuple[str, ...], ...] = (
    ("api", "key"),
    ("authorization",),
    ("password",),
    ("secret",),
    ("bearer",),
    ("token",),
    ("bearer", "credential"),
    ("token", "credential"),
)

_SAFE_KEY_SHAPES: tuple[tuple[str, ...], ...] = (
    ("token", "count"),
    ("secretary", "note"),
)

_BEARER_VALUE_RE = re.compile(
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)

_SDP_KEYS: dict[str, str] = {
    "sdp": "sdp_hash",
    "offer_sdp": "offer_sdp_hash",
    "answer_sdp": "answer_sdp_hash",
    "local_sdp": "local_sdp_hash",
    "remote_sdp": "remote_sdp_hash",
}


def _default_wall_clock() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key_components(key: str) -> tuple[str, ...]:
    delimited_key = _CAMEL_CASE_BOUNDARY_RE.sub(" ", key)
    normalized_key = re.sub(r"[_-]", " ", delimited_key)
    return tuple(part for part in normalized_key.lower().split() if part)


def _is_secret_key(key: str) -> bool:
    key_components = _key_components(key)
    if key_components in _SAFE_KEY_SHAPES:
        return False

    for shape in _SECRET_KEY_SHAPES:
        shape_len = len(shape)
        if any(
            key_components[index : index + shape_len] == shape
            for index in range(len(key_components) - shape_len + 1)
        ):
            return True

    return False


def _is_bearer_secret(value: str) -> bool:
    return bool(_BEARER_VALUE_RE.search(value))


def _validate_json_value(value: JsonValue) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return

    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise TypeError("all JSON object keys must be strings")
            _validate_json_value(item_value)
        return

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("finite JSON numbers are required")

    if isinstance(value, (str, int, float, bool)) or value is None:
        return

    raise TypeError(f"unsupported runtime JSON value type: {type(value)!r}")


def _sanitize_scalar(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return _sanitize_dict(value)
    if isinstance(value, list):
        return [_sanitize_scalar(item) for item in value]
    if isinstance(value, str) and _is_bearer_secret(value):
        return "[REDACTED]"
    return value


def _sanitize_dict(value: dict[str, JsonValue], *, is_function_payload: bool = False) -> JsonValue:
    output: dict[str, JsonValue] = {}
    for key, item in value.items():
        lowered_key = key.lower()

        if lowered_key in _SDP_KEYS and isinstance(item, str):
            output[_SDP_KEYS[lowered_key]] = _sha256_hex(item)
            continue

        if lowered_key in _CORRELATION_KEYS:
            output[key] = _sanitize_scalar(item)
            continue

        if is_function_payload and _is_secret_key(key):
            output[key] = "[REDACTED]"
            continue

        if _is_secret_key(key):
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
            sanitized[_SDP_KEYS[lowered_key]] = _sha256_hex(value)
            continue

        if lowered_key in _CORRELATION_KEYS:
            sanitized[key] = _sanitize_scalar(value)
            continue

        if _is_secret_key(key):
            sanitized[key] = "[REDACTED]"
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
        if not isinstance(payload, dict):
            raise TypeError("timing record payload must be a dict of JSON values")

        for key, value in payload.items():
            if not isinstance(key, str):
                raise TypeError("timing record payload keys must be strings")
            _validate_json_value(value)

        event = TimingEvent(
            name=name,
            monotonic_ns=monotonic_value,
            wall_time=wall_time_value,
            source=source,
            data=copy.deepcopy(payload),
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl(), encoding="utf-8")
