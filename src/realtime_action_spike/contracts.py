"""Shared strict model-facing contracts with no execution dependencies."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def parse_exact_youtube_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise ValueError("YouTube URL must use https")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("source must be a supported YouTube video URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source must be a supported YouTube video URL") from exc
    if port not in {None, 443}:
        raise ValueError("YouTube URL must use the standard https port")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_YOUTUBE_HOSTS:
        raise ValueError("source URL must be an allowlisted YouTube URL")

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
        raise ValueError("source must be a supported YouTube video URL")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


class YouTubePlayArguments(StrictArguments):
    source: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Natural-language YouTube title, channel, recency, or topic words, or one exact "
            "https YouTube video URL. Do not invent or dictate a URL for a voice request."
        ),
    )

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("source must not contain control characters")
        if is_url_shaped(value):
            parse_exact_youtube_url(value)
        return value

    @property
    def exact_url(self) -> str | None:
        if not is_url_shaped(self.source):
            return None
        return self.source
