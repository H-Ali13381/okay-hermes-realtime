from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from realtime_action_spike.media_state import (
    AnimeProgress,
    AnimeProgressStore,
    MediaPreferenceStore,
    ResumeProposal,
    propose_resume,
)


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def test_audio_mode_defaults_to_sub_and_local_overrides_do_not_mutate_default(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "nested" / "media_state.json"
    store = MediaPreferenceStore(state_path)

    assert store.get_audio_mode() == "sub"
    assert store.resolve_audio_mode("dub") == "dub"
    assert store.get_audio_mode() == "sub"

    store.set_audio_mode("dub")

    assert store.get_audio_mode() == "dub"
    assert MediaPreferenceStore(state_path).get_audio_mode() == "dub"


@pytest.mark.parametrize("mode", ["sub", "dub"])
def test_audio_mode_validation_accepts_only_sub_or_dub(tmp_path: Path, mode: str) -> None:
    store = MediaPreferenceStore(tmp_path / "state.json")

    store.set_audio_mode(mode)
    assert store.get_audio_mode() == mode
    assert store.resolve_audio_mode(None) == mode


@pytest.mark.parametrize("mode", ["", "subs", "dubs", "original"])
def test_audio_mode_validation_rejects_invalid_values(tmp_path: Path, mode: str) -> None:
    store = MediaPreferenceStore(tmp_path / "state.json")

    with pytest.raises(ValueError):
        store.set_audio_mode(mode)
    with pytest.raises(ValueError):
        store.resolve_audio_mode(mode)


def test_persistence_creates_private_dir_and_file_permissions(tmp_path: Path) -> None:
    state_path = tmp_path / "private" / "media_state.json"
    store = MediaPreferenceStore(state_path)

    store.set_audio_mode("dub")

    assert state_path.parent.exists()
    assert os.stat(state_path.parent).st_mode & 0o777 == 0o700
    assert os.stat(state_path).st_mode & 0o777 == 0o600


def test_missing_or_malformed_state_falls_back_to_defaults_without_leaking_raw_contents(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "media_state.json"
    state_path.write_text("{not valid json}", encoding="utf-8")

    pref_store = MediaPreferenceStore(state_path)
    progress_store = AnimeProgressStore(state_path)

    assert pref_store.get_audio_mode() == "sub"
    assert progress_store.all() == []
    assert progress_store.get("series") is None

    state_path.write_text(json.dumps({"version": 999, "audio_mode": "dub", "progress": {}}))

    pref_store = MediaPreferenceStore(state_path)
    progress_store = AnimeProgressStore(state_path)

    assert pref_store.get_audio_mode() == "sub"
    assert progress_store.all() == []


def test_malformed_partial_progress_is_discarded_instead_of_fabricated(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "media_state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "audio_mode": "sub",
                "progress": {
                    "frieren": {
                        "series_id": "frieren",
                        "series_title": "Frieren",
                        "completed": "yes",
                        "last_audio_mode_used": "sub",
                        "updated_at": iso_now(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert AnimeProgressStore(state_path).get("frieren") is None


def test_mismatched_stored_series_id_is_discarded(tmp_path: Path) -> None:
    state_path = tmp_path / "media_state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "audio_mode": "sub",
                "progress": {
                    "frieren": {
                        "series_id": "mha",
                        "series_title": "Frieren",
                        "last_episode": 1,
                        "position_seconds": 0,
                        "duration_seconds": 1200,
                        "completed": False,
                        "last_audio_mode_used": "sub",
                        "updated_at": iso_now(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert AnimeProgressStore(state_path).get("frieren") is None


def test_progress_constructor_validates_fields_and_normalizes_position() -> None:
    progress = AnimeProgress(
        series_id="frieren",
        series_title="Frieren: Beyond Journey's End",
        last_episode=5,
        position_seconds=999.0,
        duration_seconds=120.0,
        completed=False,
        last_audio_mode_used="sub",
        updated_at=iso_now(),
    )

    assert progress.position_seconds == 120.0

    with pytest.raises(ValueError):
        AnimeProgress(
            series_id="frieren",
            series_title="Frieren",
            last_episode=0,
            position_seconds=1.0,
            duration_seconds=10.0,
            completed=False,
            last_audio_mode_used="sub",
            updated_at=iso_now(),
        )

    with pytest.raises(ValueError):
        AnimeProgress(
            series_id="frieren",
            series_title="Frieren",
            last_episode=1,
            position_seconds=float("nan"),
            duration_seconds=10.0,
            completed=False,
            last_audio_mode_used="sub",
            updated_at=iso_now(),
        )


def test_progress_store_persists_multiple_series_and_round_trips(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "media_state.json"
    store = AnimeProgressStore(state_path)

    store.put(
        AnimeProgress(
            series_id="frieren",
            series_title="Frieren",
            last_episode=5,
            position_seconds=999.0,
            duration_seconds=1200.0,
            completed=False,
            last_audio_mode_used="sub",
            updated_at=iso_now(),
        )
    )
    store.put(
        AnimeProgress(
            series_id="mha",
            series_title="My Hero Academia",
            last_episode=1,
            position_seconds=0.0,
            duration_seconds=24.0,
            completed=True,
            last_audio_mode_used="dub",
            updated_at=iso_now(),
        )
    )

    reloaded = AnimeProgressStore(state_path)
    frieren = reloaded.get("frieren")
    mha = reloaded.get("mha")

    assert frieren is not None
    assert frieren.position_seconds == 999.0
    assert frieren.last_audio_mode_used == "sub"
    assert mha is not None
    assert mha.completed is True
    assert len(reloaded.all()) == 2


def test_progress_store_overwrites_existing_entry_and_completed_state_wins(tmp_path: Path) -> None:
    state_path = tmp_path / "media_state.json"
    store = AnimeProgressStore(state_path)

    store.put(
        AnimeProgress(
            series_id="spy-x-family",
            series_title="Spy x Family",
            last_episode=3,
            position_seconds=400.0,
            duration_seconds=1000.0,
            completed=False,
            last_audio_mode_used="sub",
            updated_at=iso_now(),
        )
    )
    store.put(
        AnimeProgress(
            series_id="spy-x-family",
            series_title="Spy x Family",
            last_episode=3,
            position_seconds=1000.0,
            duration_seconds=1000.0,
            completed=True,
            last_audio_mode_used="dub",
            updated_at=iso_now(),
        )
    )

    reloaded = AnimeProgressStore(state_path)
    progress = reloaded.get("spy-x-family")

    assert progress is not None
    assert progress.completed is True
    assert progress.position_seconds == 1000.0
    assert progress.last_audio_mode_used == "dub"


def test_concurrent_distinct_progress_updates_do_not_lose_records(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "media_state.json"
    barrier = threading.Barrier(8)

    def write(index: int) -> None:
        barrier.wait()
        AnimeProgressStore(state_path).put(
            AnimeProgress(
                series_id=f"series-{index}",
                series_title=f"Series {index}",
                last_episode=1,
                position_seconds=0,
                duration_seconds=1200,
                completed=False,
                last_audio_mode_used="sub",
                updated_at=iso_now(),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))

    assert {item.series_id for item in AnimeProgressStore(state_path).all()} == {
        f"series-{index}" for index in range(8)
    }


def test_atomic_write_fsyncs_parent_directory_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state" / "media_state.json"
    directory_fds: set[int] = set()
    fsynced: list[int] = []
    original_open = os.open
    original_fsync = os.fsync

    def recording_open(path: str | bytes, flags: int, mode: int = 0o777) -> int:
        fd = original_open(path, flags, mode)
        if Path(path) == state_path.parent:
            directory_fds.add(fd)
        return fd

    def recording_fsync(fd: int) -> None:
        fsynced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr("realtime_action_spike.media_state.os.open", recording_open)
    monkeypatch.setattr("realtime_action_spike.media_state.os.fsync", recording_fsync)

    MediaPreferenceStore(state_path).set_audio_mode("dub")

    assert directory_fds
    assert directory_fds <= set(fsynced)


def test_atomic_write_cleans_temporary_files_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state" / "media_state.json"
    store = MediaPreferenceStore(state_path)
    store.set_audio_mode("sub")

    original_replace = os.replace

    def boom(src: str, dst: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("realtime_action_spike.media_state.os.replace", boom)

    with pytest.raises(OSError, match="replace failed"):
        store.set_audio_mode("dub")

    assert state_path.exists()
    assert sorted(p.name for p in state_path.parent.iterdir()) == [state_path.name]

    monkeypatch.setattr("realtime_action_spike.media_state.os.replace", original_replace)


@pytest.mark.parametrize(
    ("position_seconds", "duration_seconds", "expected_kind", "expected_start"),
    [
        (900.0, 1000.0, "same_episode", 900.0),
        (910.0, 1000.0, "next_episode", 0.0),
        (950.0, 1000.0, "next_episode", 0.0),
        (949.0, 1000.0, "next_episode", 0.0),
        (800.0, 1000.0, "same_episode", 800.0),
    ],
)
def test_resume_policy_threshold_edges(
    position_seconds: float,
    duration_seconds: float,
    expected_kind: str,
    expected_start: float,
) -> None:
    progress = AnimeProgress(
        series_id="jujutsu-kaisen",
        series_title="Jujutsu Kaisen",
        last_episode=7,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        completed=False,
        last_audio_mode_used="sub",
        updated_at=iso_now(),
    )

    proposal = propose_resume(progress, available_episode_numbers=[7, 8])

    assert proposal.kind == expected_kind
    assert proposal.episode_number == (7 if expected_kind == "same_episode" else 8)
    assert proposal.start_seconds == expected_start
    assert proposal.reason


def test_resume_policy_uses_next_available_episode_or_returns_terminal_when_finished() -> None:
    progress = AnimeProgress(
        series_id="demon-slayer",
        series_title="Demon Slayer",
        last_episode=4,
        position_seconds=100.0,
        duration_seconds=100.0,
        completed=True,
        last_audio_mode_used="dub",
        updated_at=iso_now(),
    )

    next_episode = propose_resume(progress, available_episode_numbers=[4, 6, 9])
    terminal = propose_resume(progress, available_episode_numbers=[4])

    assert next_episode.kind == "next_episode"
    assert next_episode.episode_number == 6
    assert next_episode.start_seconds == 0.0
    assert terminal.kind == "terminal"
    assert terminal.episode_number == 4
    assert terminal.start_seconds == 0.0


def test_resume_policy_handles_new_history_and_completed_progress() -> None:
    fresh = propose_resume(None, available_episode_numbers=[3, 5])
    completed = propose_resume(
        AnimeProgress(
            series_id="vinland-saga",
            series_title="Vinland Saga",
            last_episode=2,
            position_seconds=1200.0,
            duration_seconds=1200.0,
            completed=True,
            last_audio_mode_used="sub",
            updated_at=iso_now(),
        ),
        available_episode_numbers=[2, 3],
    )

    assert fresh.kind == "start"
    assert fresh.episode_number == 3
    assert fresh.start_seconds == 0.0
    assert completed.kind == "next_episode"
    assert completed.episode_number == 3
    assert completed.start_seconds == 0.0


def test_resume_proposal_is_immutable() -> None:
    proposal = ResumeProposal(kind="same_episode", episode_number=1, start_seconds=0.0, reason="x")

    with pytest.raises(FrozenInstanceError):
        proposal.kind = "next_episode"  # type: ignore[misc]
