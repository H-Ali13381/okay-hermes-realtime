"""Controller-private pending media generations and safe display projections."""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from .contracts import parse_exact_youtube_url

AudioMode: TypeAlias = Literal["sub", "dub"]  # noqa: UP040
SourceKind: TypeAlias = Literal["youtube", "anime"]  # noqa: UP040
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class PendingMediaCatalogError(ValueError):
    """Pending media input violates the controller-owned contract."""


class StalePendingMediaError(PendingMediaCatalogError):
    """The session or pending generation is no longer current."""


def _bounded_text(value: str, name: str, maximum: int = 300) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or _CONTROL_RE.search(normalized):
        raise PendingMediaCatalogError(f"invalid {name}")
    return normalized


def _safe_id(value: str, name: str) -> str:
    if _SAFE_ID_RE.fullmatch(value) is None:
        raise PendingMediaCatalogError(f"invalid {name}")
    return value


def _https_url(value: str, name: str) -> str:
    if not value or len(value) > 2_048 or _CONTROL_RE.search(value):
        raise PendingMediaCatalogError(f"invalid {name}")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PendingMediaCatalogError(f"invalid {name}") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise PendingMediaCatalogError(f"invalid {name}")
    return value


def _optional_thumbnail(value: str | None) -> str | None:
    if value is None:
        return None
    return _https_url(value, "thumbnail URL")


@dataclass(frozen=True, slots=True)
class YouTubePlaybackLocator:
    video_id: str
    canonical_url: str

    def __post_init__(self) -> None:
        if _VIDEO_ID_RE.fullmatch(self.video_id) is None:
            raise PendingMediaCatalogError("invalid YouTube video id")
        try:
            parsed_id, canonical = parse_exact_youtube_url(self.canonical_url)
        except ValueError as exc:
            raise PendingMediaCatalogError("invalid YouTube playback URL") from exc
        if parsed_id != self.video_id or canonical != self.canonical_url:
            raise PendingMediaCatalogError("inconsistent YouTube playback locator")


_ALLOWED_HTTP_HEADERS = frozenset({"Authorization", "Referer"})


@dataclass(frozen=True, slots=True)
class DirectStreamLocator:
    stream_url: str
    webpage_url: str | None = None
    http_headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream_url", _https_url(self.stream_url, "stream URL"))
        if self.webpage_url is not None:
            object.__setattr__(
                self,
                "webpage_url",
                _https_url(self.webpage_url, "webpage URL"),
            )
        if not isinstance(self.http_headers, tuple):
            raise PendingMediaCatalogError("invalid http headers")
        bounded: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in self.http_headers:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PendingMediaCatalogError("invalid http header")
            name, value = item
            if name not in _ALLOWED_HTTP_HEADERS or name in seen:
                raise PendingMediaCatalogError("invalid http header")
            if not isinstance(value, str) or _CONTROL_RE.search(value):
                raise PendingMediaCatalogError("invalid http header")
            maximum = 2048 if name == "Authorization" else 512
            if not value or len(value) > maximum or "\r" in value or "\n" in value:
                raise PendingMediaCatalogError("invalid http header")
            seen.add(name)
            bounded.append((name, value))
        object.__setattr__(self, "http_headers", tuple(bounded))


PlaybackLocator: TypeAlias = YouTubePlaybackLocator | DirectStreamLocator  # noqa: UP040


@dataclass(frozen=True, slots=True)
class PendingEpisodeMetadata:
    series_title: str
    season_number: int | None
    episode_number: int
    episode_title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "series_title", _bounded_text(self.series_title, "series title"))
        if self.season_number is not None and (
            isinstance(self.season_number, bool) or self.season_number < 1
        ):
            raise PendingMediaCatalogError("invalid season number")
        if isinstance(self.episode_number, bool) or self.episode_number < 1:
            raise PendingMediaCatalogError("invalid episode number")
        if self.episode_title is not None:
            object.__setattr__(
                self,
                "episode_title",
                _bounded_text(self.episode_title, "episode title"),
            )


@dataclass(frozen=True, slots=True)
class PendingResumeMetadata:
    start_seconds: float
    duration_seconds: float | None
    reason: str

    def __post_init__(self) -> None:
        if self.start_seconds < 0:
            raise PendingMediaCatalogError("invalid resume position")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise PendingMediaCatalogError("invalid media duration")
        object.__setattr__(self, "reason", _bounded_text(self.reason, "resume reason"))


@dataclass(frozen=True, slots=True)
class PendingMedia:
    pending_id: str
    source: SourceKind
    title: str
    subtitle: str
    playback_locator: PlaybackLocator
    prompt: str
    thumbnail_url: str | None = None
    episode: PendingEpisodeMetadata | None = None
    audio_mode: AudioMode | None = None
    resume: PendingResumeMetadata | None = None
    request_query: str | None = None
    canonical_series_id: str | None = None
    available_episode_numbers: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pending_id", _safe_id(self.pending_id, "pending id"))
        if self.source not in {"youtube", "anime"}:
            raise PendingMediaCatalogError("invalid media source")
        if self.source == "youtube" and not isinstance(
            self.playback_locator, YouTubePlaybackLocator
        ):
            raise PendingMediaCatalogError("YouTube media requires a YouTube locator")
        if self.source == "anime" and not isinstance(self.playback_locator, DirectStreamLocator):
            raise PendingMediaCatalogError("anime media requires a direct stream locator")
        object.__setattr__(self, "title", _bounded_text(self.title, "title"))
        object.__setattr__(self, "subtitle", _bounded_text(self.subtitle, "subtitle"))
        object.__setattr__(self, "prompt", _bounded_text(self.prompt, "prompt"))
        object.__setattr__(self, "thumbnail_url", _optional_thumbnail(self.thumbnail_url))
        if self.audio_mode not in {None, "sub", "dub"}:
            raise PendingMediaCatalogError("invalid audio mode")
        if self.request_query is not None:
            object.__setattr__(
                self,
                "request_query",
                _bounded_text(self.request_query, "request query"),
            )
        if self.canonical_series_id is not None:
            object.__setattr__(
                self,
                "canonical_series_id",
                _bounded_text(self.canonical_series_id, "canonical series id"),
            )
        if any(
            isinstance(number, bool) or not isinstance(number, int) or number < 1
            for number in self.available_episode_numbers
        ):
            raise PendingMediaCatalogError("invalid available episode numbers")

    def display_projection(self) -> dict[str, object]:
        display: dict[str, object] = {
            "pending_id": self.pending_id,
            "source": self.source,
            "title": self.title,
            "subtitle": self.subtitle,
            "thumbnail_url": self.thumbnail_url,
            "prompt": self.prompt,
        }
        if self.episode is not None:
            display["episode"] = asdict(self.episode)
        if self.audio_mode is not None:
            display["audio_mode"] = self.audio_mode
        if self.resume is not None:
            display["resume"] = asdict(self.resume)
        return display


def build_youtube_thumbnail_url(video_id: str, candidate: str | None = None) -> str | None:
    if _VIDEO_ID_RE.fullmatch(video_id) is None:
        return None
    value = candidate or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    try:
        parsed = urlsplit(_https_url(value, "YouTube thumbnail URL"))
    except PendingMediaCatalogError:
        return None
    if parsed.hostname not in {"i.ytimg.com", "img.youtube.com"}:
        return None
    if not parsed.path.startswith(f"/vi/{video_id}/"):
        return None
    return value


class PendingMediaCatalog:
    """Store one current controller-private pending generation per voice session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_session: dict[str, PendingMedia] = {}

    def create_or_replace(self, session_id: str, pending: PendingMedia) -> None:
        session_id = _safe_id(session_id, "session id")
        with self._lock:
            self._by_session[session_id] = pending

    def replace_exact(
        self,
        session_id: str,
        current_pending_id: str,
        replacement: PendingMedia,
    ) -> None:
        session_id = _safe_id(session_id, "session id")
        _safe_id(current_pending_id, "pending id")
        with self._lock:
            current = self._by_session.get(session_id)
            if current is None or current.pending_id != current_pending_id:
                raise StalePendingMediaError("stale or foreign pending media generation")
            self._by_session[session_id] = replacement

    def get_exact(self, session_id: str, pending_id: str) -> PendingMedia:
        session_id = _safe_id(session_id, "session id")
        _safe_id(pending_id, "pending id")
        with self._lock:
            pending = self._by_session.get(session_id)
            if pending is None or pending.pending_id != pending_id:
                raise StalePendingMediaError("stale or foreign pending media generation")
            return pending

    def take_exact(self, session_id: str, pending_id: str) -> PendingMedia:
        with self._lock:
            pending = self.get_exact(session_id, pending_id)
            del self._by_session[session_id]
            return pending

    def clear_exact(self, session_id: str, pending_id: str) -> None:
        self.take_exact(session_id, pending_id)

    def clear_session(self, session_id: str) -> None:
        session_id = _safe_id(session_id, "session id")
        with self._lock:
            self._by_session.pop(session_id, None)
