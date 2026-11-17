from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class TimingName(StrEnum):
    PLAYBACK_SUPPRESSED = "playback_suppressed"
    NEXT_RESPONSE_FIRST_AUDIO = "next_response_first_audio"
    LISTENING_RESTORED = "listening_restored"
    PEER_CONNECTION_STATE = "peer_connection_state"
    ICE_CONNECTION_STATE = "ice_connection_state"
    ICE_GATHERING_STATE = "ice_gathering_state"
    DATA_CHANNEL_STATE = "data_channel_state"
    SDP_OFFER_CREATED = "sdp_offer_created"
    SDP_ANSWER_APPLIED = "sdp_answer_applied"
    TRANSPORT_FAILURE = "transport_failure"
    REALTIME_RESPONSE_DONE = "realtime_response_done"
    REALTIME_ERROR = "realtime_error"
    PAGE_ERROR = "page_error"


class StopReason(StrEnum):
    BUTTON = "button"
    MODEL_REQUEST = "model_request"
    TRANSPORT_FAILURE = "transport_failure"
    NATIVE_CANCEL = "native_cancel"
    TIMEOUT = "timeout"


class SessionOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskEventKind(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    GAVE_UP = "gave_up"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    BLOCK_LOOP_DETECTED = "block_loop_detected"


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{12,128}$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_RESPONSE_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_PLAYBACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SECRET_KEY_NAMES = {
    "api_key",
    "authorization",
    "bearer",
    "token",
    "password",
    "secret",
    "call_id",
}


class _StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActivationMessage(_StrictBaseModel):
    type: Literal["activation"] = "activation"
    probability: float
    detected_at: float
    native_listener: bool

    @field_validator("probability", mode="before")
    @classmethod
    def _validate_probability(cls, value: float) -> float:
        if not isinstance(value, float):
            raise ValueError("probability must be a float")
        if not math.isfinite(value):
            raise ValueError("probability must be finite")
        if not (0.0 <= value <= 1.0):
            raise ValueError("probability must be in the range [0, 1]")
        return value

    @field_validator("detected_at", mode="before")
    @classmethod
    def _validate_detected_at(cls, value: float) -> float:
        if not isinstance(value, float):
            raise ValueError("detected_at must be a float")
        if not math.isfinite(value):
            raise ValueError("detected_at must be finite")
        if value <= 0.0:
            raise ValueError("detected_at must be > 0")
        return value

    @field_validator("native_listener", mode="before")
    @classmethod
    def _validate_native_listener(cls, value: bool) -> bool:
        if not isinstance(value, bool):
            raise ValueError("native_listener must be a bool")
        return value


class PageReadyMessage(_StrictBaseModel):
    type: Literal["page_ready"] = "page_ready"
    session_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)


class PageStartedMessage(_StrictBaseModel):
    type: Literal["page_started"] = "page_started"
    session_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)


class TimingMessage(_StrictBaseModel):
    type: Literal["timing"] = "timing"
    session_id: str
    name: TimingName
    monotonic_ms: float
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)

    @field_validator("monotonic_ms", mode="before")
    @classmethod
    def _validate_monotonic_ms(cls, value: float | int) -> float:
        # JSON cannot distinguish 360 from 360.0 — the browser's
        # Number(x.toFixed(2)) serializes whole values as integers. Accept
        # ints and coerce; bools and other types are still rejected.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("monotonic_ms must be a number")
        coerced = float(value)
        if not math.isfinite(coerced):
            raise ValueError("monotonic_ms must be finite")
        if coerced < 0.0:
            raise ValueError("monotonic_ms must be >= 0")
        return coerced

    @field_validator("name", mode="before")
    @classmethod
    def _coerce_timing_name(cls, value: str | TimingName) -> TimingName:
        return TimingName(value)

    @model_validator(mode="after")
    def _validate_data(self) -> TimingMessage:
        _validate_timing_message_data(self.name, self.data)
        return self


class StopMessage(_StrictBaseModel):
    type: Literal["stop"] = "stop"
    session_id: str
    reason: StopReason

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)


class TeardownCompleteMessage(_StrictBaseModel):
    type: Literal["teardown_complete"] = "teardown_complete"
    session_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)


class SessionClosedMessage(_StrictBaseModel):
    type: Literal["session_closed"] = "session_closed"
    session_id: str
    outcome: SessionOutcome

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)


class ContinuationItem(_StrictBaseModel):
    role: Literal["user", "assistant"]
    text: str

    @field_validator("text", mode="before")
    @classmethod
    def _validate_text(cls, value: Any) -> str:
        return _normalize_media_text(value, field_name="text")


class _MediaSessionPlaybackMessage(_StrictBaseModel):
    session_id: str
    playback_id: str

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)

    @field_validator("playback_id")
    @classmethod
    def _validate_playback_id(cls, value: str) -> str:
        return _validate_playback_id(value)


class MediaReadyMessage(_MediaSessionPlaybackMessage):
    type: Literal["media_ready"] = "media_ready"
    history: list[ContinuationItem]

    @model_validator(mode="after")
    def _validate_history(self) -> MediaReadyMessage:
        if len(self.history) > 24:
            raise ValueError("history must contain at most 24 items")
        total_text = sum(len(item.text) for item in self.history)
        if total_text > 16000:
            raise ValueError("history text must be at most 16000 characters")
        return self


class MediaCancelMessage(_MediaSessionPlaybackMessage):
    type: Literal["media_cancel"] = "media_cancel"
    reason: Literal[
        "confirmation_failed",
        "confirmation_timed_out",
        "browser_failure",
    ]


class MediaFinishedMessage(_MediaSessionPlaybackMessage):
    type: Literal["media_finished"] = "media_finished"
    outcome: Literal["completed", "failed"]
    next_episode_pending: bool = False


class MediaResumeCompleteMessage(_MediaSessionPlaybackMessage):
    type: Literal["media_resume_complete"] = "media_resume_complete"
    mode: Literal["exact", "reconstructed"]


class MediaEpisodeDisplay(_StrictBaseModel):
    series_title: str = Field(min_length=1, max_length=300)
    season_number: int | None = Field(default=None, ge=1)
    episode_number: int = Field(ge=1)
    episode_title: str | None = Field(default=None, min_length=1, max_length=300)


class MediaResumeDisplay(_StrictBaseModel):
    start_seconds: float = Field(ge=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=300)


class MediaConfirmationMessage(_StrictBaseModel):
    type: Literal["media_confirmation"] = "media_confirmation"
    session_id: str
    pending_id: str
    source: Literal["youtube", "anime"]
    title: str = Field(min_length=1, max_length=300)
    subtitle: str = Field(min_length=1, max_length=300)
    thumbnail_url: str | None = Field(default=None, max_length=2048)
    prompt: str = Field(min_length=1, max_length=300)
    episode: MediaEpisodeDisplay | None = None
    audio_mode: Literal["sub", "dub"] | None = None
    resume: MediaResumeDisplay | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)

    @field_validator("pending_id")
    @classmethod
    def _validate_pending_id(cls, value: str) -> str:
        return _validate_playback_id(value)

    @field_validator("title", "subtitle", "prompt", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any, info: Any) -> str:
        return _normalize_media_text(value, field_name=info.field_name)

    @field_validator("thumbnail_url")
    @classmethod
    def _validate_thumbnail_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {
                "i.ytimg.com",
                "img.youtube.com",
                "cdn.xlsbox.com",
                "cdn.anipixcdn.co",
            }
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError("thumbnail_url must use an allowlisted HTTPS image host")
        return value


class AnimeChoiceDisplay(_StrictBaseModel):
    position: int = Field(ge=1, le=5)
    series_id: str = Field(min_length=3, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$")
    title: str = Field(min_length=1, max_length=300)


class MediaChoicesMessage(_StrictBaseModel):
    type: Literal["media_choices"] = "media_choices"
    session_id: str
    query: str = Field(min_length=1, max_length=300)
    pending_ids: tuple[str, ...] = Field(min_length=2, max_length=5)
    choices: tuple[AnimeChoiceDisplay, ...] = Field(min_length=2, max_length=5)

    @field_validator("pending_ids", "choices", mode="before")
    @classmethod
    def _coerce_sequences(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(value.split())
        return value

    @field_validator("pending_ids")
    @classmethod
    def _validate_pending_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _validate_playback_id(item)
        return value

    @field_validator("choices")
    @classmethod
    def _validate_choices_alignment(
        cls,
        value: tuple[AnimeChoiceDisplay, ...],
        info: Any,
    ) -> tuple[AnimeChoiceDisplay, ...]:
        pending_ids = info.data.get("pending_ids")
        if pending_ids is not None and len(pending_ids) != len(value):
            raise ValueError("pending_ids and choices must align one-to-one")
        for index, choice in enumerate(value, start=1):
            if choice.position != index:
                raise ValueError("choice positions must ascend from 1")
        return value


class TaskEventMessage(_StrictBaseModel):
    """Controller-to-browser relay for an exact tracked Kanban event."""

    type: Literal["task_event"] = "task_event"
    session_id: str
    event_id: int = Field(ge=1)
    task_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    kind: TaskEventKind
    title: str = Field(min_length=1, max_length=160)
    detail: str | None = Field(default=None, max_length=400)
    requires_user_input: bool = False
    block_kind: Literal["dependency", "needs_input", "transient", "capability"] | None = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        return _validate_session_id(value)

    @field_validator("title", "detail")
    @classmethod
    def _validate_bounded_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 and character not in {"\t", "\n"} for character in value):
            raise ValueError("task event text contains control characters")
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("task event text must not be blank")
        return normalized

    @model_validator(mode="after")
    def _validate_block_shape(self) -> TaskEventMessage:
        if self.kind is not TaskEventKind.BLOCKED and (
            self.requires_user_input or self.block_kind is not None
        ):
            raise ValueError("only blocked task events may request user input")
        return self


LoopbackMessage = Annotated[
    ActivationMessage
    | PageReadyMessage
    | PageStartedMessage
    | TimingMessage
    | StopMessage
    | TeardownCompleteMessage
    | SessionClosedMessage
    | MediaReadyMessage
    | MediaCancelMessage
    | MediaFinishedMessage
    | MediaResumeCompleteMessage
    | MediaConfirmationMessage
    | MediaChoicesMessage
    | TaskEventMessage,
    Field(discriminator="type"),
]


_LOOPBACK_ADAPTER: TypeAdapter[LoopbackMessage] = TypeAdapter(LoopbackMessage)


def _validate_session_id(value: str) -> str:
    if not _SESSION_ID_RE.fullmatch(value):
        raise ValueError("session_id must be 12..128 chars in [A-Za-z0-9_-]")

    lowered = value.lower()
    if lowered.startswith("call_") or lowered.startswith("session_"):
        raise ValueError("session_id must not be provider-looking")

    return value


def _validate_playback_id(value: str) -> str:
    if not isinstance(value, str) or not _PLAYBACK_ID_RE.fullmatch(value):
        raise ValueError("playback_id must be 8..128 chars in [A-Za-z0-9_-]")
    return value


def _normalize_media_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if any(
        (ord(character) < 32 or ord(character) == 127)
        and character not in {"\t", "\n", "\r", "\f", "\v"}
        for character in value
    ):
        raise ValueError(f"{field_name} must not contain disallowed control characters")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > 16000:
        raise ValueError(f"{field_name} must be at most 16000 characters")
    return normalized


def _normalize_secret_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower()


def _contains_secret_key(key: str) -> bool:
    normalized_key = _normalize_secret_key(key)
    if normalized_key in _SECRET_KEY_NAMES:
        return True

    parts = [part for part in normalized_key.split("_") if part]
    return any(part in _SECRET_KEY_NAMES for part in parts)


def _scan_no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _contains_secret_key(str(key)):
                raise ValueError(f"forbidden key in timing payload: {key}")
            _scan_no_secrets(nested)
        return

    if isinstance(value, list):
        for item in value:
            _scan_no_secrets(item)
        return

    if value is not None and not isinstance(value, (str, int, float, bool, dict, list)):
        raise ValueError("timing payload must be valid JSON data")

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("timing payload numeric values must be finite")


def _validate_timing_message_data(name: TimingName, data: dict[str, Any]) -> None:
    _scan_no_secrets(data)

    if name == TimingName.PLAYBACK_SUPPRESSED:
        allowed = {"suppressed", "response_id"}
        provided = set(data.keys())
        if unknown := provided - allowed:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "suppressed" in data and not isinstance(data["suppressed"], bool):
            raise ValueError("suppressed must be a bool")
        if "response_id" in data:
            _validate_provider_response_id(data["response_id"], "response_id")
        return

    if name in {
        TimingName.NEXT_RESPONSE_FIRST_AUDIO,
        TimingName.LISTENING_RESTORED,
    }:
        required = {"response_id", "interrupted_response_id"}
        provided = set(data.keys())
        if unknown := provided - required:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if missing := required - provided:
            raise ValueError(f"response_id fields are required: {sorted(missing)}")
        _validate_provider_response_id(data["response_id"], "response_id")
        _validate_provider_response_id(
            data["interrupted_response_id"],
            "interrupted_response_id",
        )
        return

    if name == TimingName.PEER_CONNECTION_STATE:
        provided = set(data.keys())
        if unknown := provided - {"state"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "state" not in data:
            raise ValueError("state is required")
        if data["state"] not in {
            "new",
            "connecting",
            "connected",
            "disconnected",
            "failed",
            "closed",
        }:
            raise ValueError("invalid peer_connection_state state")
        if not isinstance(data["state"], str):
            raise ValueError("state must be a string")
        return

    if name == TimingName.ICE_CONNECTION_STATE:
        provided = set(data.keys())
        if unknown := provided - {"state"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "state" not in data:
            raise ValueError("state is required")
        if data["state"] not in {
            "new",
            "checking",
            "connected",
            "completed",
            "failed",
            "disconnected",
            "closed",
        }:
            raise ValueError("invalid ice_connection_state state")
        if not isinstance(data["state"], str):
            raise ValueError("state must be a string")
        return

    if name == TimingName.ICE_GATHERING_STATE:
        provided = set(data.keys())
        if unknown := provided - {"state"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "state" not in data:
            raise ValueError("state is required")
        if data["state"] not in {"new", "gathering", "complete"}:
            raise ValueError("invalid ice_gathering_state state")
        if not isinstance(data["state"], str):
            raise ValueError("state must be a string")
        return

    if name == TimingName.PAGE_ERROR:
        provided = set(data.keys())
        if unknown := provided - {"kind", "message"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "kind" not in data or "message" not in data:
            raise ValueError("kind and message are required")
        if data["kind"] not in {"error", "unhandledrejection"}:
            raise ValueError("invalid page_error kind")
        if not isinstance(data["message"], str):
            raise ValueError("message must be a string")
        if len(data["message"]) > 512:
            raise ValueError("message is too long")
        return

    if name == TimingName.DATA_CHANNEL_STATE:
        provided = set(data.keys())
        if unknown := provided - {"state"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "state" not in data:
            raise ValueError("state is required")
        if data["state"] not in {"connecting", "open", "closing", "closed"}:
            raise ValueError("invalid data_channel_state state")
        if not isinstance(data["state"], str):
            raise ValueError("state must be a string")
        return

    if name in {TimingName.SDP_OFFER_CREATED, TimingName.SDP_ANSWER_APPLIED}:
        provided = set(data.keys())
        if unknown := provided - {"sdp_hash"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")

        for key in provided:
            if key in {"sdp", "offer_sdp", "answer_sdp"}:
                raise ValueError(f"raw SDP fields are not allowed: {key}")

        if "sdp_hash" in data:
            value = data["sdp_hash"]
            if not isinstance(value, str):
                raise ValueError("sdp_hash must be a string")
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("sdp_hash must be lowercase 64-char sha256 hex")
        return

    if name == TimingName.TRANSPORT_FAILURE:
        provided = set(data.keys())
        if unknown := provided - {"state"}:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if "state" not in data:
            raise ValueError("state is required")
        if data["state"] not in {"disconnected", "failed", "closed"}:
            raise ValueError("invalid transport_failure state")
        if not isinstance(data["state"], str):
            raise ValueError("state must be a string")
        return

    if name == TimingName.REALTIME_RESPONSE_DONE:
        allowed = {
            "response_id",
            "status",
            "output_types",
            "suppressed_response_id",
            "pending_restore_response_id",
            "remote_audio_muted",
        }
        provided = set(data.keys())
        if unknown := provided - allowed:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        required = {"response_id", "status", "output_types", "remote_audio_muted"}
        if missing := required - provided:
            raise ValueError(f"response diagnostic fields are required: {sorted(missing)}")
        _validate_provider_response_id(data["response_id"], "response_id")
        for field_name in ("suppressed_response_id", "pending_restore_response_id"):
            if field_name in data:
                _validate_provider_response_id(data[field_name], field_name)
        if data["status"] not in {"completed", "cancelled", "failed", "incomplete"}:
            raise ValueError("invalid realtime response status")
        _validate_diagnostic_strings(data["output_types"], "output_types", max_items=16)
        if not isinstance(data["remote_audio_muted"], bool):
            raise ValueError("remote_audio_muted must be a bool")
        return

    if name == TimingName.REALTIME_ERROR:
        allowed = {"error_type", "code", "message"}
        provided = set(data.keys())
        if unknown := provided - allowed:
            raise ValueError(f"unexpected timing data keys: {sorted(unknown)}")
        if not provided:
            raise ValueError("at least one realtime error field is required")
        for field_name in provided:
            max_length = 512 if field_name == "message" else 128
            _validate_diagnostic_text(data[field_name], field_name, max_length=max_length)
        return

    raise ValueError(f"unsupported timing name: {name}")


def _validate_provider_response_id(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _PROVIDER_RESPONSE_ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded provider response identifier")


def _validate_diagnostic_text(value: Any, field_name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")


def _validate_diagnostic_strings(value: Any, field_name: str, *, max_items: int) -> None:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field_name} must be a bounded list")
    for item in value:
        _validate_diagnostic_text(item, field_name, max_length=64)


def parse_loopback_message(
    payload: str | bytes | Mapping[str, object],
    *,
    expected_session_id: str | None = None,
) -> LoopbackMessage:
    if isinstance(payload, Mapping):
        parsed = _LOOPBACK_ADAPTER.validate_python(payload)
    elif isinstance(payload, (str, bytes)):
        parsed = _LOOPBACK_ADAPTER.validate_json(payload)
    else:
        raise TypeError("payload must be string, bytes, or mapping")

    if (
        expected_session_id is not None
        and not isinstance(parsed, ActivationMessage)
        and parsed.session_id != expected_session_id
    ):
        raise ValueError("session_id mismatch")

    return parsed


def encode_loopback_message(message: LoopbackMessage) -> str:
    return json.dumps(
        message.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
