from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

STATE_VERSION = 1
AudioMode = Literal["sub", "dub"]
ResumeKind = Literal["start", "same_episode", "next_episode", "terminal"]
_SERIES_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True, slots=True)
class AnimeProgress:
    series_id: str
    series_title: str
    last_episode: int
    position_seconds: float
    duration_seconds: float
    completed: bool
    last_audio_mode_used: AudioMode
    updated_at: str

    def __post_init__(self) -> None:
        series_id = _canonical_series_id(self.series_id)
        series_title = self.series_title.strip()
        last_episode = _coerce_positive_int(self.last_episode, "last_episode")
        position_seconds = _coerce_finite_nonnegative_float(
            self.position_seconds, "position_seconds"
        )
        duration_seconds = _coerce_finite_positive_float(self.duration_seconds, "duration_seconds")
        completed = bool(self.completed)
        last_audio_mode_used = _validate_audio_mode(self.last_audio_mode_used)
        updated_at = _validate_iso_timestamp(self.updated_at)
        position_seconds = min(position_seconds, duration_seconds)

        if not series_id:
            raise ValueError("series_id must not be empty")
        if not series_title:
            raise ValueError("series_title must not be empty")

        object.__setattr__(self, "series_id", series_id)
        object.__setattr__(self, "series_title", series_title)
        object.__setattr__(self, "last_episode", last_episode)
        object.__setattr__(self, "position_seconds", position_seconds)
        object.__setattr__(self, "duration_seconds", duration_seconds)
        object.__setattr__(self, "completed", completed)
        object.__setattr__(self, "last_audio_mode_used", last_audio_mode_used)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class ResumeProposal:
    kind: ResumeKind
    episode_number: int
    start_seconds: float
    reason: str


@dataclass(slots=True)
class MediaPreferenceStore:
    path: Path

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get_audio_mode(self) -> AudioMode:
        state = _load_state(self.path)
        value = state.get("audio_mode")
        return _validate_audio_mode(value) if isinstance(value, str) else "sub"

    def resolve_audio_mode(self, override: str | None) -> AudioMode:
        if override is None:
            return self.get_audio_mode()
        return _validate_audio_mode(override)

    def set_audio_mode(self, audio_mode: str) -> None:
        mode = _validate_audio_mode(audio_mode)
        with _state_lock(self.path):
            state = _load_state(self.path)
            state["version"] = STATE_VERSION
            state["audio_mode"] = mode
            state.setdefault("progress", {})
            _write_state(self.path, state)


@dataclass(slots=True)
class AnimeProgressStore:
    path: Path

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def get(self, series_id: str) -> AnimeProgress | None:
        canonical = _canonical_series_id(series_id)
        if not canonical:
            raise ValueError("series_id must not be empty")
        state = _load_state(self.path)
        progress_map = state.get("progress")
        if not isinstance(progress_map, dict):
            return None
        payload = progress_map.get(canonical)
        if not isinstance(payload, dict):
            return None
        try:
            series_title = payload.get("series_title")
            stored_series_id = payload.get("series_id")
            last_episode = payload.get("last_episode")
            position_seconds = payload.get("position_seconds")
            duration_seconds = payload.get("duration_seconds")
            completed = payload.get("completed")
            last_audio_mode_used = payload.get("last_audio_mode_used")
            updated_at = payload.get("updated_at")
            if (
                not isinstance(series_title, str)
                or not isinstance(stored_series_id, str)
                or _canonical_series_id(stored_series_id) != canonical
                or isinstance(last_episode, bool)
                or not isinstance(last_episode, int)
                or isinstance(position_seconds, bool)
                or not isinstance(position_seconds, (int, float))
                or isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (int, float))
                or not isinstance(completed, bool)
                or not isinstance(last_audio_mode_used, str)
                or not isinstance(updated_at, str)
            ):
                return None
            return AnimeProgress(
                series_id=canonical,
                series_title=series_title,
                last_episode=last_episode,
                position_seconds=position_seconds,
                duration_seconds=duration_seconds,
                completed=completed,
                last_audio_mode_used=_validate_audio_mode(last_audio_mode_used),
                updated_at=updated_at,
            )
        except (TypeError, ValueError):
            return None

    def all(self) -> list[AnimeProgress]:
        state = _load_state(self.path)
        progress_map = state.get("progress")
        if not isinstance(progress_map, dict):
            return []
        items: list[AnimeProgress] = []
        for series_id in sorted(progress_map):
            if not isinstance(series_id, str):
                continue
            progress = self.get(series_id)
            if progress is not None:
                items.append(progress)
        return items

    def put(self, progress: AnimeProgress) -> None:
        with _state_lock(self.path):
            state = _load_state(self.path)
            state["version"] = STATE_VERSION
            state.setdefault("audio_mode", "sub")
            progress_map = state.get("progress")
            if not isinstance(progress_map, dict):
                progress_map = {}
            canonical = progress.series_id
            progress_map[canonical] = {
                "series_id": progress.series_id,
                "series_title": progress.series_title,
                "last_episode": progress.last_episode,
                "position_seconds": progress.position_seconds,
                "duration_seconds": progress.duration_seconds,
                "completed": progress.completed,
                "last_audio_mode_used": progress.last_audio_mode_used,
                "updated_at": progress.updated_at,
            }
            state["progress"] = progress_map
            _write_state(self.path, state)


def propose_resume(
    progress: AnimeProgress | None,
    available_episode_numbers: list[int] | tuple[int, ...] | set[int] | None = None,
) -> ResumeProposal:
    available = _normalize_episode_numbers(available_episode_numbers)
    if progress is None:
        episode = _first_episode_or_one(available)
        reason = "no history; start from the first available episode"
        return ResumeProposal(
            kind="start", episode_number=episode, start_seconds=0.0, reason=reason
        )

    if progress.completed:
        return _next_or_terminal(
            progress,
            available,
            reason="history is complete; advance to the next available episode",
        )

    remaining_seconds = progress.duration_seconds - progress.position_seconds
    remaining_fraction = remaining_seconds / progress.duration_seconds
    if remaining_fraction > 0.05 and remaining_seconds > 90.0:
        return ResumeProposal(
            kind="same_episode",
            episode_number=progress.last_episode,
            start_seconds=progress.position_seconds,
            reason="enough of the episode remains; resume where playback stopped",
        )

    return _next_or_terminal(
        progress,
        available,
        reason="too little remains; advance to the next available episode",
    )


def _next_or_terminal(
    progress: AnimeProgress,
    available: list[int],
    *,
    reason: str,
) -> ResumeProposal:
    next_episode = next((number for number in available if number > progress.last_episode), None)
    if next_episode is None:
        return ResumeProposal(
            kind="terminal",
            episode_number=progress.last_episode,
            start_seconds=0.0,
            reason="no later episode is available; remain on the current finished episode",
        )
    return ResumeProposal(
        kind="next_episode",
        episode_number=next_episode,
        start_seconds=0.0,
        reason=reason,
    )


def _first_episode_or_one(available: list[int]) -> int:
    return available[0] if available else 1


def _normalize_episode_numbers(
    available_episode_numbers: list[int] | tuple[int, ...] | set[int] | None,
) -> list[int]:
    if available_episode_numbers is None:
        return []
    normalized_set: set[int] = set()
    for number in available_episode_numbers:
        if isinstance(number, bool):
            continue
        try:
            value = int(number)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized_set.add(value)
    normalized = sorted(normalized_set)
    return normalized


def _canonical_series_id(series_id: str) -> str:
    canonical = series_id.strip().lower()
    if not _SERIES_ID_RE.fullmatch(canonical):
        raise ValueError("series_id must be a canonical lowercase slug")
    return canonical


def _state_lock(path: Path) -> threading.RLock:
    canonical = path.expanduser().resolve()
    with _LOCKS_GUARD:
        return _STATE_LOCKS.setdefault(canonical, threading.RLock())


def _validate_audio_mode(value: str) -> AudioMode:
    mode = value.strip().lower()
    if mode not in {"sub", "dub"}:
        raise ValueError("audio_mode must be 'sub' or 'dub'")
    return cast(AudioMode, mode)


def _coerce_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _coerce_finite_nonnegative_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a finite non-negative number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


def _coerce_finite_positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a finite positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return number


def _validate_iso_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("updated_at must be an ISO timestamp string")
    candidate = value.strip()
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated_at must be an ISO timestamp string") from exc
    return candidate


def _default_state() -> dict[str, object]:
    return {"version": STATE_VERSION, "audio_mode": "sub", "progress": {}}


def _load_state(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default_state()
    except OSError:
        return _default_state()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _default_state()
    if not isinstance(payload, dict):
        return _default_state()
    if payload.get("version") != STATE_VERSION:
        return _default_state()

    state = _default_state()
    audio_mode = payload.get("audio_mode")
    if isinstance(audio_mode, str):
        try:
            state["audio_mode"] = _validate_audio_mode(audio_mode)
        except ValueError:
            state["audio_mode"] = "sub"
    progress = payload.get("progress")
    if isinstance(progress, dict):
        state["progress"] = progress
    return state


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise
