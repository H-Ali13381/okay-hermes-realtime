"""Shared strict model-facing contracts with no execution dependencies."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_URL_SHAPE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://|^www\.|^[^\s/]+\.[^\s/]+(?:/|$)",
    re.IGNORECASE,
)


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def is_url_shaped(value: str) -> bool:
    return _URL_SHAPE_RE.search(value) is not None


def is_explicit_url(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) is not None


def _normalize_text_value(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _validate_control_chars(field_name: str, value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def parse_exact_youtube_url(value: str, field_name: str = "query") -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise ValueError("YouTube URL must use https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{field_name} must be a supported YouTube video URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a supported YouTube video URL") from exc
    if port not in {None, 443}:
        raise ValueError("YouTube URL must use the standard https port")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_YOUTUBE_HOSTS:
        raise ValueError(f"{field_name} URL must be an allowlisted YouTube URL")

    video_id: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1:
            video_id = parts[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            candidates = parse_qs(parsed.query).get("v", [])
            if len(candidates) == 1:
                video_id = candidates[0]
        elif len(parts) == 2 and parts[0] == "shorts":
            video_id = parts[1]

    if video_id is None or _VIDEO_ID_RE.fullmatch(video_id) is None:
        raise ValueError(f"{field_name} must be a supported YouTube video URL")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"




class MediaPrepareYouTubeArguments(StrictArguments):
    query: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Natural-language YouTube title, channel, recency, or topic words, or one exact "
            "https YouTube video URL. This prepares a proposal only and does not play media."
        ),
    )

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: object) -> object:
        return _normalize_text_value(value)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        _validate_control_chars("query", value)
        if is_explicit_url(value):
            parse_exact_youtube_url(value, "query")
        return value

    @property
    def exact_url(self) -> str | None:
        if not is_explicit_url(self.query):
            return None
        return self.query


class MediaPrepareAnimeArguments(StrictArguments):
    query: str = Field(
        min_length=1,
        max_length=300,
        description="Anime title, series, or episode request to prepare as a proposal.",
    )
    episode: int | None = Field(default=None, ge=1, description="Specific episode number.")
    episode_offset: int | None = Field(
        default=None,
        ge=-100,
        le=100,
        description="Relative episode offset from the current point; cannot be 0.",
    )
    audio_mode: Literal["auto", "sub", "dub"] = Field(
        default="auto",
        description="Audio mode preference, defaulting to auto.",
    )

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: object) -> object:
        return _normalize_text_value(value)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        return _validate_control_chars("query", value)

    @field_validator("episode_offset")
    @classmethod
    def _validate_episode_offset(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("episode_offset cannot be 0")
        return value

    @model_validator(mode="after")
    def _validate_episode_selection(self) -> MediaPrepareAnimeArguments:
        if self.episode is not None and self.episode_offset is not None:
            raise ValueError("episode and episode_offset are mutually exclusive")
        return self


class MediaRevisionArguments(StrictArguments):
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        description="Optional revised media query; may be relative to the current pending plan.",
    )
    episode: int | None = Field(default=None, ge=1, description="Specific episode number.")
    episode_offset: int | None = Field(
        default=None,
        ge=-100,
        le=100,
        description="Relative episode offset from the current point; cannot be 0.",
    )
    audio_mode: Literal["auto", "sub", "dub"] | None = Field(
        default=None,
        description="Optional revised audio mode preference.",
    )

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: object) -> object:
        return _normalize_text_value(value)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_control_chars("query", value)
        if is_url_shaped(value):
            parse_exact_youtube_url(value, "query")
        return value

    @field_validator("episode_offset")
    @classmethod
    def _validate_episode_offset(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("episode_offset cannot be 0")
        return value

    @model_validator(mode="after")
    def _validate_revision_fields(self) -> MediaRevisionArguments:
        if self.episode is not None and self.episode_offset is not None:
            raise ValueError("episode and episode_offset are mutually exclusive")
        if all(
            value is None
            for value in (self.query, self.episode, self.episode_offset, self.audio_mode)
        ):
            raise ValueError("revision must include at least one field")
        return self


class MediaDecisionArguments(StrictArguments):
    pending_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Pending media proposal id.",
    )
    action: Literal["accept", "reject", "cancel", "revise"] = Field(
        description="Accept, reject, cancel, or revise the pending media proposal.",
    )
    revision: MediaRevisionArguments | None = Field(
        default=None,
        description="Optional revision details required only when action is revise.",
    )

    @model_validator(mode="after")
    def _validate_revision_requirement(self) -> MediaDecisionArguments:
        if self.action == "revise":
            if self.revision is None:
                raise ValueError("revision is required when action is revise")
        elif self.revision is not None:
            raise ValueError("revision is only allowed when action is revise")
        return self
