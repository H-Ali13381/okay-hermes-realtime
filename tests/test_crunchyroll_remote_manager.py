from __future__ import annotations

import asyncio

import pytest

from realtime_action_spike.crunchyroll_remote.manager import CrunchyrollLoginManager


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, **kwargs: object) -> None:
        ready = kwargs.pop("ready")
        assert isinstance(ready, asyncio.Event)
        self.calls.append(kwargs)
        self.started.set()
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_manager_starts_one_exact_session_and_rejects_duplicate() -> None:
    runner = RecordingRunner()
    manager = CrunchyrollLoginManager(
        brave_binary="/opt/brave-origin-bin/brave",
        profile_path="/tmp/crunchyroll-profile",
        runner=runner,
    )

    result = await manager.start(
        session_id="voice-session-01",
        query="Cowboy Bebop on Crunchyroll",
        episode=2,
        audio_mode="dub",
    )
    await asyncio.wait_for(runner.started.wait(), timeout=1)

    assert result == {
        "status": "authentication_required",
        "provider": "crunchyroll",
        "login_window_opened": True,
        "request_query": "Cowboy Bebop on Crunchyroll",
        "message": "Scan the QR code in the Crunchyroll window to sign in.",
    }
    assert runner.calls == [
        {
            "session_id": "voice-session-01",
            "brave_binary": "/opt/brave-origin-bin/brave",
            "profile_path": "/tmp/crunchyroll-profile",
            "timeout_seconds": 300.0,
        }
    ]
    with pytest.raises(ValueError, match="already active"):
        await manager.start(
            session_id="voice-session-01",
            query="Other title on Crunchyroll",
            episode=None,
            audio_mode="auto",
        )

    await manager.stop_session("voice-session-01")
    assert runner.cancelled.is_set()
    assert manager.active_session_id is None


@pytest.mark.asyncio
async def test_manager_rejects_other_session_and_shutdown_reaps_active_runner() -> None:
    runner = RecordingRunner()
    manager = CrunchyrollLoginManager(
        brave_binary="brave",
        profile_path="/tmp/profile",
        runner=runner,
    )
    await manager.start(
        session_id="voice-session-01",
        query="Frieren on Crunchyroll",
        episode=None,
        audio_mode="sub",
    )
    await runner.started.wait()

    with pytest.raises(ValueError, match="another voice session"):
        await manager.start(
            session_id="voice-session-02",
            query="Frieren on Crunchyroll",
            episode=None,
            audio_mode="sub",
        )

    await manager.shutdown()
    assert runner.cancelled.is_set()
    assert manager.active_session_id is None


@pytest.mark.asyncio
async def test_manager_clears_naturally_completed_runner() -> None:
    finished = asyncio.Event()
    release = asyncio.Event()

    async def runner(**kwargs: object) -> None:
        ready = kwargs["ready"]
        assert isinstance(ready, asyncio.Event)
        ready.set()
        await release.wait()
        finished.set()

    manager = CrunchyrollLoginManager(
        brave_binary="brave",
        profile_path="/tmp/profile",
        runner=runner,
    )
    await manager.start(
        session_id="voice-session-01",
        query="Frieren on Crunchyroll",
        episode=None,
        audio_mode="auto",
    )
    release.set()
    await finished.wait()
    await asyncio.sleep(0)

    assert manager.active_session_id is None
