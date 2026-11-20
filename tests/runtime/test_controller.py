from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from realtime_action_spike.anime import AnimeResolution, RankedResolution
from realtime_action_spike.capabilities import HandoffRecord
from realtime_action_spike.media import (
    LaunchedPlayback,
    MediaPlaybackManager,
    PlaybackCompletion,
    PlaybackProgress,
    YouTubeResolver,
    YouTubeVideo,
)
from realtime_action_spike.media_state import AnimeProgressStore, MediaPreferenceStore
from realtime_action_spike.openai.interruption import (
    InterruptionEvent,
    InterruptionEventKind,
)
from realtime_action_spike.runtime.browser import BrowserHandle, NoopBrowserHandle
from realtime_action_spike.runtime.controller import (
    SessionOutcome,
    StaleControlMessage,
    TerminalSessionResult,
    VoiceSessionController,
)
from realtime_action_spike.runtime.protocol import (
    MediaConfirmationMessage,
    MediaFinishedMessage,
    MediaReadyMessage,
    MediaResumeCompleteMessage,
    PageReadyMessage,
    PageStartedMessage,
    SessionClosedMessage,
    StopMessage,
    StopReason,
    TaskEventKind,
    TaskEventMessage,
    TeardownCompleteMessage,
    TimingMessage,
    TimingName,
    encode_loopback_message,
)
from realtime_action_spike.runtime.tokens import LaunchTokenStore


@pytest.fixture(autouse=True)
def isolate_controller_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class FakeBrowserHandle(NoopBrowserHandle):
    pass


class DeterministicLauncher:
    def __init__(self) -> None:
        self.launched_urls: list[str] = []
        self.handles: list[NoopBrowserHandle] = []

    def launch(self, loopback_url: str) -> BrowserHandle:
        self.launched_urls.append(loopback_url)
        handle = FakeBrowserHandle()
        self.handles.append(handle)
        return handle


class FailingLauncher:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def launch(self, loopback_url: str) -> BrowserHandle:
        self.calls += 1
        raise self.error


class FailingCloseHandle(NoopBrowserHandle):
    def close(self) -> None:
        self.closed_calls += 1
        raise RuntimeError("injected browser close failure")


class FirstCloseFailsLauncher(DeterministicLauncher):
    def launch(self, loopback_url: str) -> BrowserHandle:
        self.launched_urls.append(loopback_url)
        handle: FakeBrowserHandle | FailingCloseHandle = (
            FailingCloseHandle() if not self.handles else FakeBrowserHandle()
        )
        self.handles.append(handle)
        return handle


class LockReleasingLauncher(DeterministicLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.release_calls = 0

    def release_profile_lock(self) -> None:
        self.release_calls += 1


class SequenceFactory:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        value = self._values[self._index]
        self._index += 1
        return value


class RecordingMediaManager:
    def __init__(self) -> None:
        self.launch_calls: list[dict[str, object]] = []
        self.shutdown_calls = 0
        self.progress_handler = None
        self.completion_handler = None

    @property
    def status(self) -> dict[str, object]:
        return {"status": "playing"} if self.launch_calls else {"status": "idle"}

    async def launch(self, **kwargs: object) -> LaunchedPlayback:
        self.launch_calls.append(kwargs)
        return LaunchedPlayback(
            playback_id=str(kwargs["playback_id"]),
            title=str(kwargs["title"]),
            subtitle=str(kwargs["subtitle"]),
        )

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    def bind_progress_handler(self, handler) -> None:
        self.progress_handler = handler

    def bind_completion_handler(self, handler) -> None:
        self.completion_handler = handler

    async def emit_progress(self, sample: PlaybackProgress) -> None:
        assert self.progress_handler is not None
        await self.progress_handler(sample)

    async def emit_completion(self, completion: PlaybackCompletion) -> None:
        assert self.completion_handler is not None
        await self.completion_handler(completion)


class RecordingYouTubeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, source: str) -> YouTubeVideo:
        self.calls.append(source)
        return YouTubeVideo(
            video_id="dQw4w9WgXcQ",
            title="Selected title",
            channel="Selected channel",
            webpage_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )


class BlockingSecondYouTubeResolver(RecordingYouTubeResolver):
    def __init__(self) -> None:
        super().__init__()
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()

    async def resolve(self, source: str) -> YouTubeVideo:
        if self.calls:
            self.second_started.set()
            await self.release_second.wait()
        return await super().resolve(source)


class RecordingAnimeAdapter:
    def __init__(self, *, http_headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.calls: list[dict[str, object]] = []
        self.http_headers = http_headers

    async def resolve(self, query: str, **kwargs: object) -> AnimeResolution:
        self.calls.append({"query": query, **kwargs})
        episode = int(kwargs.get("episode", 1))
        mode = kwargs.get("audio_mode", "auto")
        resolved_mode = "sub" if mode == "auto" else str(mode)
        return AnimeResolution(
            series_id="cowboy-bebop-1065",
            series_title="Cowboy Bebop",
            episode=episode,
            audio_mode=resolved_mode,  # type: ignore[arg-type]
            subtitle=f"Episode {episode} · {'Subbed' if resolved_mode == 'sub' else 'Dubbed'}",
            poster_url="https://cdn.xlsbox.com/poster/small/1065.jpg",
            stream_url=f"https://hls.anidb.app/stream/example/ep-{episode}.m3u8",
            http_headers=self.http_headers,
            available_episode_numbers=(1, 2, 3, 4),
        )

    async def resolve_or_rank(self, query: str, **kwargs: object):
        resolution = await self.resolve(query, **kwargs)
        return RankedResolution(kind="resolved", resolution=resolution, choices=())


class RecordingCrunchyrollLoginManager:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.stop_calls: list[str] = []
        self.shutdown_calls = 0

    async def start(self, **kwargs: object) -> dict[str, object]:
        self.start_calls.append(kwargs)
        return {
            "status": "authentication_required",
            "provider": "crunchyroll",
            "login_window_opened": True,
            "request_query": kwargs["query"],
            "message": "Scan the QR code in the Crunchyroll window to sign in.",
        }

    async def stop_session(self, session_id: str) -> None:
        self.stop_calls.append(session_id)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class RecordingTaskEvents:
    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []
        self.resolutions: list[dict[str, object]] = []
        self.shutdown_calls = 0

    def register_task(self, **kwargs: object) -> bool:
        self.registrations.append(kwargs)
        return True

    async def resolve_permission(self, **kwargs: object) -> dict[str, object]:
        self.resolutions.append(kwargs)
        return {"status": "unblocked", **kwargs}

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def interruption_controller() -> VoiceSessionController:
    return VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-interruption"])),
        session_id_factory=SequenceFactory(["local-interruption-01"]),
    )


@pytest.mark.asyncio
async def test_activate_is_single_session_under_concurrency() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2", "token-3"]),
        ),
        session_id_factory=SequenceFactory(
            ["local-session-01", "local-session-02", "local-session-03"]
        ),
    )

    statuses = await asyncio.gather(
        *(controller.activate("http://127.0.0.1:8765/voice") for _ in range(3))
    )

    opened = [status for status in statuses if status.status == "opened"]
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_busy_activation_is_reported_and_launcher_not_called_for_second_request() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01", "local-session-02"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    second = await controller.activate("http://127.0.0.1:8765/voice")

    assert first.status == "opened"
    assert first.session_id == "local-session-01"
    assert second.status == "busy"
    assert len(launcher.handles) == 1


@pytest.mark.asyncio
async def test_task_events_are_session_bound_and_permission_resolution_delegates() -> None:
    task_events = RecordingTaskEvents()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-task-event"])),
        session_id_factory=SequenceFactory(["local-task-event-01"]),
        task_event_coordinator=task_events,  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-task-event-01"
    message = TaskEventMessage(
        session_id=session_id,
        event_id=11,
        task_id="t_voice01",
        kind=TaskEventKind.BLOCKED,
        title="Voice handoff: probe",
        detail="May I overwrite config.toml?",
        requires_user_input=True,
        block_kind="needs_input",
    )

    assert await controller.publish_task_event(session_id, message) is True
    assert await controller.wait_for_outbound_message(session_id) == message
    assert await controller.publish_task_event("other-session-01", message) is False

    assert (
        controller.register_handoff(
            session_id,
            HandoffRecord(
                task_id="t_voice01",
                title="Voice handoff: probe",
                board_slug="default",
                db_path="/tmp/kanban.db",
            ),
        )
        is True
    )
    assert task_events.registrations == [
        {
            "session_id": session_id,
            "task_id": "t_voice01",
            "title": "Voice handoff: probe",
            "board_slug": "default",
            "db_path": "/tmp/kanban.db",
        }
    ]

    result = await controller.resolve_task_permission(
        session_id=session_id,
        task_id="t_voice01",
        block_event_id=11,
        decision="approve_once",
        response="Yes, that one file.",
    )
    assert result["status"] == "unblocked"
    assert task_events.resolutions[0]["block_event_id"] == 11

    await controller.shutdown_task_events()
    assert task_events.shutdown_calls == 1


@pytest.mark.asyncio
async def test_crunchyroll_login_is_bound_to_live_session_and_reaped_on_teardown() -> None:
    login = RecordingCrunchyrollLoginManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-crunchyroll"])),
        session_id_factory=SequenceFactory(["local-crunchyroll-01"]),
        crunchyroll_login_manager=login,  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-crunchyroll-01"

    with pytest.raises(StaleControlMessage, match="originating session"):
        await controller.prepare_crunchyroll_media(
            session_id="other-session",
            query="Cowboy Bebop on Crunchyroll",
            episode=2,
            audio_mode="dub",
        )

    result = await controller.prepare_crunchyroll_media(
        session_id=session_id,
        query="Cowboy Bebop on Crunchyroll",
        episode=2,
        audio_mode="dub",
    )
    assert result["status"] == "authentication_required"
    assert login.start_calls == [
        {
            "session_id": session_id,
            "query": "Cowboy Bebop on Crunchyroll",
            "episode": 2,
            "audio_mode": "dub",
        }
    ]

    await controller.close_active_session()
    assert login.stop_calls == [session_id]
    await controller.shutdown_crunchyroll()
    assert login.shutdown_calls == 1


@pytest.mark.asyncio
async def test_controller_shares_media_manager_youtube_resolver() -> None:
    youtube = YouTubeResolver(yt_dlp_bin="/opt/bin/yt-dlp")
    controller = VoiceSessionController(
        DeterministicLauncher(),
        media_manager=MediaPlaybackManager(resolver=youtube),  # type: ignore[arg-type]
    )

    assert controller._youtube_resolver is youtube


@pytest.mark.asyncio
async def test_media_prepare_publishes_card_and_accept_launches_exactly_once() -> None:
    media = RecordingMediaManager()
    youtube = RecordingYouTubeResolver()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-media"])),
        session_id_factory=SequenceFactory(["local-media-01"]),
        pending_id_factory=SequenceFactory(["pending-youtube-01"]),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=youtube,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-media-01"

    result = await controller.prepare_youtube_media(
        session_id=session_id,
        query="latest useful video",
    )
    outbound = await controller.wait_for_outbound_message(session_id)

    assert result == {
        "status": "awaiting_confirmation",
        "pending_id": "pending-youtube-01",
        "prompt": "Play Selected title by Selected channel?",
    }
    assert outbound == MediaConfirmationMessage(
        session_id=session_id,
        pending_id="pending-youtube-01",
        source="youtube",
        title="Selected title",
        subtitle="Selected channel",
        thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        prompt="Play Selected title by Selected channel?",
    )
    assert media.launch_calls == []

    accepted = await controller.decide_media(
        session_id=session_id,
        pending_id="pending-youtube-01",
        action="accept",
        revision=None,
    )
    assert accepted == {
        "status": "handoff_pending",
        "pending_id": "pending-youtube-01",
        "media_handoff": True,
    }
    assert media.launch_calls == []
    await controller.process_control_message(
        session_id,
        MediaReadyMessage(
            session_id=session_id,
            playback_id="pending-youtube-01",
            history=[],
        ).model_dump_json(),
    )
    assert len(media.launch_calls) == 1

    with pytest.raises(StaleControlMessage, match="stale media handoff"):
        await controller.process_control_message(
            session_id,
            MediaReadyMessage(
                session_id=session_id,
                playback_id="pending-youtube-01",
                history=[],
            ).model_dump_json(),
        )

    with pytest.raises(ValueError, match="stale"):
        await controller.decide_media(
            session_id=session_id,
            pending_id="pending-youtube-01",
            action="accept",
            revision=None,
        )
    assert len(media.launch_calls) == 1


@pytest.mark.asyncio
async def test_anime_http_headers_reach_confirmed_playback_locator() -> None:
    media = RecordingMediaManager()
    anime = RecordingAnimeAdapter(
        http_headers=(
            ("Authorization", "Bearer token-value"),
            ("Referer", "https://www.example.test/"),
        )
    )
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-headers"])),
        session_id_factory=SequenceFactory(["local-headers-01"]),
        pending_id_factory=SequenceFactory(["pending-headers-01"]),
        media_manager=media,  # type: ignore[arg-type]
        anime_adapter=anime,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="sub",
    )
    await controller.decide_media(
        session_id=session_id,
        pending_id="pending-headers-01",
        action="accept",
        revision=None,
    )
    await controller.process_control_message(
        session_id,
        MediaReadyMessage(
            session_id=session_id,
            playback_id="pending-headers-01",
            history=[],
        ).model_dump_json(),
    )

    assert len(media.launch_calls) == 1
    locator = media.launch_calls[0]["locator"]
    assert locator.http_headers == (
        ("Authorization", "Bearer token-value"),
        ("Referer", "https://www.example.test/"),
    )


@pytest.mark.asyncio
async def test_anime_revise_replaces_generation_and_reject_never_launches() -> None:
    media = RecordingMediaManager()
    anime = RecordingAnimeAdapter()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-anime"])),
        session_id_factory=SequenceFactory(["local-anime-01"]),
        pending_id_factory=SequenceFactory(
            ["pending-anime-01", "pending-anime-02", "pending-anime-03"]
        ),
        media_manager=media,  # type: ignore[arg-type]
        anime_adapter=anime,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-anime-01"

    first = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="auto",
    )
    await controller.wait_for_outbound_message(session_id)
    revised = await controller.decide_media(
        session_id=session_id,
        pending_id=str(first["pending_id"]),
        action="revise",
        revision={"episode_offset": 1, "audio_mode": "dub"},
    )
    outbound = await controller.wait_for_outbound_message(session_id)

    assert revised["status"] == "awaiting_confirmation"
    assert revised["pending_id"] == "pending-anime-02"
    assert outbound.pending_id == "pending-anime-02"  # type: ignore[union-attr]
    assert outbound.subtitle == "Episode 2 · Dubbed"  # type: ignore[union-attr]
    assert anime.calls[-1] == {
        "query": "Cowboy Bebop",
        "episode": 2,
        "audio_mode": "dub",
    }
    with pytest.raises(ValueError, match="stale"):
        await controller.decide_media(
            session_id=session_id,
            pending_id="pending-anime-01",
            action="accept",
            revision=None,
        )

    rejected = await controller.decide_media(
        session_id=session_id,
        pending_id="pending-anime-02",
        action="reject",
        revision=None,
    )
    assert rejected == {
        "status": "rejected",
        "pending_id": "pending-anime-02",
        "follow_up": "Ask one short question about what the user wants instead.",
    }
    assert controller.session_is_active(session_id) is True
    assert media.launch_calls == []

    replacement = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=3,
        episode_offset=None,
        audio_mode="sub",
    )
    cancelled = await controller.decide_media(
        session_id=session_id,
        pending_id=str(replacement["pending_id"]),
        action="cancel",
        revision=None,
    )
    assert cancelled == {
        "status": "cancelled",
        "pending_id": "pending-anime-03",
    }


@pytest.mark.asyncio
async def test_late_revision_cannot_replace_an_already_accepted_generation() -> None:
    resolver = BlockingSecondYouTubeResolver()
    media = RecordingMediaManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-revision-race"])),
        session_id_factory=SequenceFactory(["local-revision-race"]),
        pending_id_factory=SequenceFactory(["pending-race-01", "pending-race-02"]),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=resolver,  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-revision-race"
    first = await controller.prepare_youtube_media(session_id=session_id, query="first")
    await controller.wait_for_outbound_message(session_id)

    revision_task = asyncio.create_task(
        controller.decide_media(
            session_id=session_id,
            pending_id=str(first["pending_id"]),
            action="revise",
            revision={"query": "replacement"},
        )
    )
    await resolver.second_started.wait()
    accepted = await controller.decide_media(
        session_id=session_id,
        pending_id=str(first["pending_id"]),
        action="accept",
        revision=None,
    )
    resolver.release_second.set()

    assert accepted["status"] == "handoff_pending"
    with pytest.raises(ValueError, match="stale"):
        await revision_task
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(controller.wait_for_outbound_message(session_id), timeout=0.01)
    await controller.process_control_message(
        session_id,
        MediaReadyMessage(
            session_id=session_id,
            playback_id="pending-race-01",
            history=[],
        ).model_dump_json(),
    )
    assert len(media.launch_calls) == 1


@pytest.mark.asyncio
async def test_anime_without_history_or_episode_defaults_explicitly_to_episode_one() -> None:
    anime = RecordingAnimeAdapter()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-default-episode"])),
        session_id_factory=SequenceFactory(["local-default-episode"]),
        pending_id_factory=SequenceFactory(["pending-default-episode"]),
        media_manager=RecordingMediaManager(),  # type: ignore[arg-type]
        anime_adapter=anime,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")

    result = await controller.prepare_anime_media(
        session_id=activation.session_id,
        query="Cowboy Bebop",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )

    assert result["status"] == "awaiting_confirmation"
    assert anime.calls == [{"query": "Cowboy Bebop", "episode": 1, "audio_mode": "sub"}]


@pytest.mark.asyncio
async def test_explicit_anime_mode_updates_global_preference_and_auto_reuses_it(
    tmp_path: Path,
) -> None:
    anime = RecordingAnimeAdapter()
    preference_store = MediaPreferenceStore(tmp_path / "media_state.json")
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-preference"])),
        session_id_factory=SequenceFactory(["local-preference"]),
        pending_id_factory=SequenceFactory(["pending-pref-01", "pending-pref-02"]),
        media_manager=RecordingMediaManager(),  # type: ignore[arg-type]
        anime_adapter=anime,
        media_preference_store=preference_store,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-preference"

    await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="dub",
    )
    assert preference_store.get_audio_mode() == "dub"
    await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=2,
        episode_offset=None,
        audio_mode="auto",
    )
    assert anime.calls[-1]["audio_mode"] == "dub"


@pytest.mark.asyncio
async def test_failed_teardown_clears_pending_media_without_launching() -> None:
    media = RecordingMediaManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-media-abort"])),
        session_id_factory=SequenceFactory(["local-media-abort"]),
        pending_id_factory=SequenceFactory(["pending-abort-01"]),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=RecordingYouTubeResolver(),
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-media-abort"
    await controller.prepare_youtube_media(session_id=session_id, query="video")

    await controller.request_teardown(
        session_id,
        outcome=SessionOutcome.FAILED,
        reason=StopReason.TRANSPORT_FAILURE,
    )

    assert media.launch_calls == []
    with pytest.raises(ValueError, match="originating session"):
        await controller.decide_media(
            session_id=session_id,
            pending_id="pending-abort-01",
            action="accept",
            revision=None,
        )


@pytest.mark.asyncio
async def test_teardown_clears_accepted_handoff_before_media_ready() -> None:
    media = RecordingMediaManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-handoff-abort"])),
        session_id_factory=SequenceFactory(["local-handoff-abort"]),
        pending_id_factory=SequenceFactory(["pending-handoff-abort"]),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=RecordingYouTubeResolver(),  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-handoff-abort"
    await controller.prepare_youtube_media(session_id=session_id, query="video")
    await controller.decide_media(
        session_id=session_id,
        pending_id="pending-handoff-abort",
        action="accept",
        revision=None,
    )
    await controller.request_teardown(
        session_id,
        outcome=SessionOutcome.FAILED,
        reason=StopReason.TRANSPORT_FAILURE,
    )

    assert media.launch_calls == []
    with pytest.raises(StaleControlMessage, match="no active session"):
        await controller.process_control_message(
            session_id,
            MediaReadyMessage(
                session_id=session_id,
                playback_id="pending-handoff-abort",
                history=[],
            ).model_dump_json(),
        )


@pytest.mark.asyncio
async def test_anime_progress_and_near_eof_queue_next_episode_without_autoplay(
    tmp_path: Path,
) -> None:
    media = RecordingMediaManager()
    anime = RecordingAnimeAdapter()
    progress_store = AnimeProgressStore(tmp_path / "media_state.json")
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-anime-eof"])),
        session_id_factory=SequenceFactory(["local-anime-eof"]),
        pending_id_factory=SequenceFactory(["pending-anime-eof-01", "pending-anime-eof-02"]),
        media_manager=media,  # type: ignore[arg-type]
        anime_adapter=anime,
        anime_progress_store=progress_store,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-anime-eof"
    first = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="auto",
    )
    await controller.wait_for_outbound_message(session_id)
    await controller.decide_media(
        session_id=session_id,
        pending_id=str(first["pending_id"]),
        action="accept",
        revision=None,
    )
    await controller.process_control_message(
        session_id,
        MediaReadyMessage(
            session_id=session_id,
            playback_id="pending-anime-eof-01",
            history=[],
        ).model_dump_json(),
    )

    await media.emit_progress(
        PlaybackProgress(
            session_id=session_id,
            playback_id="pending-anime-eof-01",
            position_seconds=600,
            duration_seconds=1800,
        )
    )
    checkpoint = progress_store.get("cowboy-bebop-1065")
    assert checkpoint is not None
    assert checkpoint.completed is False
    assert checkpoint.position_seconds == 600

    await media.emit_completion(
        PlaybackCompletion(
            session_id=session_id,
            playback_id="pending-anime-eof-01",
            title="Cowboy Bebop",
            channel="Episode 1 · Subbed",
            outcome="completed",
            position_seconds=1785,
            duration_seconds=1800,
        )
    )
    finished = await controller.wait_for_outbound_message(session_id)
    next_card = await controller.wait_for_outbound_message(session_id)

    assert finished == MediaFinishedMessage(
        session_id=session_id,
        playback_id="pending-anime-eof-01",
        outcome="completed",
        next_episode_pending=True,
    )
    assert isinstance(next_card, MediaConfirmationMessage)
    assert next_card.pending_id == "pending-anime-eof-02"
    assert next_card.subtitle == "Episode 2 · Subbed"
    assert len(media.launch_calls) == 1
    completed = progress_store.get("cowboy-bebop-1065")
    assert completed is not None and completed.completed is True


@pytest.mark.asyncio
async def test_manual_partial_close_preserves_progress_and_does_not_queue_next_episode(
    tmp_path: Path,
) -> None:
    media = RecordingMediaManager()
    progress_store = AnimeProgressStore(tmp_path / "media_state.json")
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-anime-close"])),
        session_id_factory=SequenceFactory(["local-anime-close"]),
        pending_id_factory=SequenceFactory(["pending-anime-close-01"]),
        media_manager=media,  # type: ignore[arg-type]
        anime_adapter=RecordingAnimeAdapter(),
        anime_progress_store=progress_store,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-anime-close"
    first = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="auto",
    )
    await controller.wait_for_outbound_message(session_id)
    await controller.decide_media(
        session_id=session_id,
        pending_id=str(first["pending_id"]),
        action="accept",
        revision=None,
    )
    await controller.process_control_message(
        session_id,
        MediaReadyMessage(
            session_id=session_id,
            playback_id="pending-anime-close-01",
            history=[],
        ).model_dump_json(),
    )
    await media.emit_completion(
        PlaybackCompletion(
            session_id=session_id,
            playback_id="pending-anime-close-01",
            title="Cowboy Bebop",
            channel="Episode 1 · Subbed",
            outcome="completed",
            position_seconds=300,
            duration_seconds=1800,
        )
    )

    finished = await controller.wait_for_outbound_message(session_id)
    assert finished == MediaFinishedMessage(
        session_id=session_id,
        playback_id="pending-anime-close-01",
        outcome="completed",
        next_episode_pending=False,
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(controller.wait_for_outbound_message(session_id), timeout=0.01)
    progress = progress_store.get("cowboy-bebop-1065")
    assert progress is not None
    assert progress.completed is False
    assert progress.position_seconds == 300

    await controller.process_control_message(
        session_id,
        PageStartedMessage(session_id=session_id).model_dump_json(),
    )
    await controller.process_control_message(
        session_id,
        MediaResumeCompleteMessage(
            session_id=session_id,
            playback_id="pending-anime-close-01",
            mode="reconstructed",
        ).model_dump_json(),
    )
    with pytest.raises(StaleControlMessage, match="stale media resume"):
        await controller.process_control_message(
            session_id,
            MediaResumeCompleteMessage(
                session_id=session_id,
                playback_id="pending-anime-close-01",
                mode="reconstructed",
            ).model_dump_json(),
        )


@pytest.mark.asyncio
async def test_shutdown_media_delegates_to_singleton_manager() -> None:
    media = RecordingMediaManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        media_manager=media,  # type: ignore[arg-type]
    )

    await controller.shutdown_media()

    assert media.shutdown_calls == 1


@pytest.mark.asyncio
async def test_failed_activation_invalidates_token_and_releases_slot() -> None:
    launcher = FailingLauncher(RuntimeError("launch failed"))
    token_factory = SequenceFactory(["token-fail"])
    session_factory = SequenceFactory(["session-fail"])
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=token_factory),
        session_id_factory=session_factory,
    )

    result = await controller.activate("http://127.0.0.1:8765/voice")
    assert result.status == "failed"
    assert result.session_id == "session-fail"
    assert result.token == "token-fail"
    assert controller.token_store.validate("token-fail") is None
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_control_message_flow_tracks_state_and_idempotent_stop_teardown() -> None:
    launcher = DeterministicLauncher()
    observed_statuses: list[str] = []
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01"]),
        status_observer=observed_statuses.append,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    assert controller.status == "launching"

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    assert await controller.process_control_message(session_id, ready) is None
    assert controller.status == "connecting"

    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    assert await controller.process_control_message(session_id, started) is None
    assert controller.status == "live"

    marker = encode_loopback_message(
        TimingMessage(
            type="timing",
            session_id=session_id,
            name=TimingName.PEER_CONNECTION_STATE,
            monotonic_ms=12.0,
            data={"state": "connected"},
        )
    )
    assert await controller.process_control_message(session_id, marker) is None

    stop = encode_loopback_message(
        StopMessage(type="stop", session_id=session_id, reason=StopReason.BUTTON)
    )
    assert await controller.process_control_message(session_id, stop) is None
    assert controller.status == "stopping"
    assert await controller.process_control_message(session_id, stop) is None

    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    closed_first = await controller.process_control_message(session_id, teardown)
    closed_second = await controller.process_control_message(session_id, teardown)

    assert isinstance(closed_first, SessionClosedMessage)
    assert isinstance(closed_second, SessionClosedMessage)
    assert closed_first.session_id == session_id
    assert closed_second.session_id == session_id
    assert closed_first.outcome == closed_second.outcome
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"
    assert observed_statuses == ["launching", "connecting", "live", "stopping", "idle"]


@pytest.mark.asyncio
async def test_browser_timing_after_controller_event_uses_controller_clock_order() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-timing"])),
        session_id_factory=SequenceFactory(["local-session-timing"]),
        pending_id_factory=SequenceFactory(["pending-anime-timing"]),
        anime_adapter=RecordingAnimeAdapter(),  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    await controller.process_control_message(
        session_id,
        encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id)),
    )
    await controller.process_control_message(
        session_id,
        encode_loopback_message(PageStartedMessage(type="page_started", session_id=session_id)),
    )
    await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="sub",
    )

    marker = TimingMessage(
        type="timing",
        session_id=session_id,
        name=TimingName.REALTIME_RESPONSE_DONE,
        monotonic_ms=9_349.1,
        data={
            "response_id": "resp-after-prepare",
            "status": "completed",
            "output_types": ["message"],
            "remote_audio_muted": False,
        },
    )

    assert (
        await controller.process_control_message(session_id, encode_loopback_message(marker))
        is None
    )
    await controller.close_active_session()


@pytest.mark.asyncio
async def test_teardown_releases_profile_lock_after_browser_close() -> None:
    launcher = LockReleasingLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-lock"])),
        session_id_factory=SequenceFactory(["local-session-lock"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    # Any terminal path (button, X-close, native cancel) converges here.
    await controller.close_active_session()

    assert controller.status == "idle"
    assert launcher.handles[0].closed_calls == 1
    # The profile lock is released exactly once, after the browser was closed.
    assert launcher.release_calls == 1


@pytest.mark.asyncio
async def test_realtime_diagnostics_are_logged_while_session_is_active(caplog) -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-log"])),
        session_id_factory=SequenceFactory(["local-session-log"]),
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    caplog.set_level("INFO", logger="realtime_action_spike.runtime.controller")

    marker = encode_loopback_message(
        TimingMessage(
            type="timing",
            session_id=session_id,
            name=TimingName.REALTIME_ERROR,
            monotonic_ms=12.0,
            data={"error_type": "provider_error", "message": "response failed"},
        )
    )
    assert await controller.process_control_message(session_id, marker) is None

    diagnostic_records = [
        record.message for record in caplog.records if "session_diagnostic" in record.message
    ]
    assert len(diagnostic_records) == 1
    assert '"name":"realtime_error"' in diagnostic_records[0]
    assert '"message":"response failed"' in diagnostic_records[0]
    assert "token-log" not in diagnostic_records[0]
    await controller.close_active_session()


@pytest.mark.asyncio
async def test_stale_session_messages_are_rejected_after_newer_session_starts() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-a", "token-b"]),
        ),
        session_id_factory=SequenceFactory(["local-session-aa", "local-session-bb"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    first_session_id = first.session_id
    assert first_session_id is not None

    started = encode_loopback_message(
        PageReadyMessage(type="page_ready", session_id=first_session_id)
    )
    live = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=first_session_id)
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=first_session_id)
    )

    assert await controller.process_control_message(first_session_id, started) is None
    assert await controller.process_control_message(first_session_id, live) is None
    assert isinstance(
        await controller.process_control_message(first_session_id, teardown),
        SessionClosedMessage,
    )

    second = await controller.activate("http://127.0.0.1:8765/voice")
    second_session_id = second.session_id
    assert second_session_id == "local-session-bb"

    with pytest.raises(StaleControlMessage):
        await controller.process_control_message(
            first_session_id,
            encode_loopback_message(
                StopMessage(
                    type="stop",
                    session_id=first_session_id,
                    reason=StopReason.BUTTON,
                )
            ),
        )

    assert second_session_id is not None
    assert controller.active_session_id == second_session_id
    assert second_session_id == "local-session-bb"
    assert launcher.handles[0].closed_calls == 1
    assert launcher.handles[1].closed_calls == 0


@pytest.mark.asyncio
async def test_activation_creates_distinct_session_id_and_launch_token() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-distinct"]),
        ),
        session_id_factory=SequenceFactory(["session-distinct"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")

    assert activation.status == "opened"
    assert activation.session_id == "session-distinct"
    assert activation.token == "token-distinct"
    assert activation.token != activation.session_id


@pytest.mark.asyncio
async def test_wait_for_terminal_result_returns_completed_outcome() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-terminal-1"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    waiter = asyncio.create_task(controller.wait_for_terminal_result(session_id))

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    assert await controller.process_control_message(session_id, ready) is None
    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    await controller.process_control_message(session_id, started)
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    await controller.process_control_message(session_id, teardown)

    result = await waiter
    assert result == TerminalSessionResult(
        session_id=session_id,
        outcome=SessionOutcome.COMPLETED,
        error=None,
    )


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_terminal_future() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-terminal-2"])),
        session_id_factory=SequenceFactory(["local-session-02"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    waiter = asyncio.create_task(controller.wait_for_terminal_result(session_id))
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    await controller.process_control_message(session_id, ready)
    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    stop_msg = encode_loopback_message(
        StopMessage(type="stop", session_id=session_id, reason=StopReason.TRANSPORT_FAILURE)
    )
    await controller.process_control_message(session_id, started)
    await controller.process_control_message(session_id, stop_msg)
    await controller.process_control_message(session_id, teardown)

    result = await controller.wait_for_terminal_result(session_id)
    assert result.outcome == SessionOutcome.FAILED


@pytest.mark.asyncio
async def test_browser_startup_timeout_closes_browser_and_releases_slot() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-startup-timeout"]),
        ),
        session_id_factory=SequenceFactory(["session-startup-timeout"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    result = await asyncio.wait_for(
        controller.wait_for_terminal_result(activation.session_id),
        timeout=0.5,
    )

    assert result == TerminalSessionResult(
        session_id="session-startup-timeout",
        outcome=SessionOutcome.TIMED_OUT,
        error="browser startup timed out",
    )
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_page_started_cancels_browser_startup_timeout() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-startup-live"]),
        ),
        session_id_factory=SequenceFactory(["session-startup-live"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageReadyMessage(type="page_ready", session_id=activation.session_id)
        ),
    )
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=activation.session_id)
        ),
    )

    await asyncio.sleep(0.03)

    assert controller.status == "live"
    assert launcher.handles[0].closed_calls == 0
    await controller.close_active_session()
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_page_started_before_page_ready_is_accepted_and_cancels_startup_timeout() -> None:
    """The browser may observe session.updated before its page_ready handshake.

    The Realtime session can acknowledge over the data channel before the
    control websocket finishes its page_ready turn (e.g. fast reconnect, or
    the provider answering session.updated ahead of the loopback handshake).
    The controller must still accept page_started and cancel the startup
    deadline; rejecting it would reintroduce the 20s "activation failed"
    timeout family on an otherwise healthy session.
    """
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-started-first"]),
        ),
        session_id_factory=SequenceFactory(["session-started-first"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    # page_started arrives while the session is still in LAUNCHING (no page_ready).
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=activation.session_id)
        ),
    )

    await asyncio.sleep(0.03)

    assert controller.status == "live"
    assert launcher.handles[0].closed_calls == 0
    await controller.close_active_session()
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_stuck_at_connecting_expires_as_timed_out_and_teardown_is_bounded() -> None:
    """Regression replay of the observed stuck-at-connecting failure family.

    Real trace pattern (e.g. Z2DkHx8cuaq0GAFyJA0odA): the browser connects the
    control socket, sends page_ready, creates the SDP offer and applies the
    answer, then the peer connection stalls at "connecting" — page_started
    never arrives because session.updated is never acknowledged. The startup
    deadline must expire the session as timed_out, teardown must complete in
    bounded time even though the browser never acknowledges the stop, and the
    controller must return to idle so the wakeword listener can re-arm.
    """
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-stuck", "token-follow-up"]),
        ),
        session_id_factory=SequenceFactory(["session-stuck", "session-follow-up"]),
        browser_start_timeout_seconds=0.02,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.02,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    # Browser reaches CONNECTING (page_ready) and traces the observed events:
    # SDP offer, answer, peer stuck at "connecting" — but never page_started.
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageReadyMessage(type="page_ready", session_id=activation.session_id)
        ),
    )
    for name, data in ((TimingName.PEER_CONNECTION_STATE, {"state": "connecting"}),):
        await controller.process_control_message(
            activation.session_id,
            encode_loopback_message(
                TimingMessage(
                    type="timing",
                    session_id=activation.session_id,
                    name=name,
                    monotonic_ms=1.0,
                    data=data,
                )
            ),
        )

    terminal = await asyncio.wait_for(
        controller.wait_for_terminal_result(activation.session_id),
        timeout=2.0,
    )

    assert terminal.outcome == SessionOutcome.TIMED_OUT
    assert controller.status == "idle"
    assert launcher.handles[0].closed_calls >= 1

    # The session slot must be free for the next wake activation.
    follow_up = await controller.activate("http://127.0.0.1:8765/voice")
    assert follow_up.status == "opened"
    await controller.close_active_session()


@pytest.mark.asyncio
async def test_launch_failure_resolves_terminal_result_and_releases_slot() -> None:
    launcher = FailingLauncher(RuntimeError("launch failed"))
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-fail-2"])),
        session_id_factory=SequenceFactory(["session-fail-2"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.status == "failed"

    result = await controller.wait_for_terminal_result(activation.session_id or "")
    assert result == TerminalSessionResult(
        session_id="session-fail-2",
        outcome=SessionOutcome.FAILED,
        error="activation failed",
    )
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_stale_control_message_cannot_resolve_newer_session_terminal_future() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2"]),
        ),
        session_id_factory=SequenceFactory(["session-first-01", "session-second-02"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    first_id = first.session_id or ""

    await controller.process_control_message(
        first_id,
        encode_loopback_message(PageReadyMessage(type="page_ready", session_id=first_id)),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(PageStartedMessage(type="page_started", session_id=first_id)),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            StopMessage(type="stop", session_id=first_id, reason=StopReason.BUTTON)
        ),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=first_id)
        ),
    )

    first_result = await controller.wait_for_terminal_result(first_id)
    assert first_result.outcome == SessionOutcome.COMPLETED

    stale_stop = encode_loopback_message(
        StopMessage(type="stop", session_id=first_id, reason=StopReason.BUTTON)
    )
    with pytest.raises(StaleControlMessage):
        await controller.process_control_message(first_id, stale_stop)

    second = await controller.activate("http://127.0.0.1:8765/voice")
    second_id = second.session_id or ""
    await controller.process_control_message(
        second_id,
        encode_loopback_message(PageReadyMessage(type="page_ready", session_id=second_id)),
    )

    second_waiter = asyncio.create_task(controller.wait_for_terminal_result(second_id))

    await controller.process_control_message(
        second_id,
        encode_loopback_message(PageStartedMessage(type="page_started", session_id=second_id)),
    )
    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            StopMessage(type="stop", session_id=second_id, reason=StopReason.BUTTON)
        ),
    )
    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=second_id)
        ),
    )

    result = await second_waiter
    assert result.outcome == SessionOutcome.COMPLETED


@pytest.mark.asyncio
async def test_controller_owns_interruption_reducer_for_exact_active_session() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-interruption-01"

    await controller.begin_realtime_response(
        session_id,
        "resp-1",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )
    accepted = await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
            user_speech_onset_ms=30.0,
        ),
    )

    traces = await controller.interruption_traces(session_id)
    assert accepted is True
    assert len(traces) == 1
    assert traces[0].response_id == "resp-1"
    assert traces[0].provider_audio_start_ms == 25
    assert traces[0].speech_started_received_ns == 2_000


@pytest.mark.asyncio
async def test_controller_rejects_stale_session_and_response_interruption_events() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    await controller.begin_realtime_response(
        session_id,
        "resp-current",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )

    with pytest.raises(StaleControlMessage):
        await controller.record_interruption_event(
            "stale-session-000",
            InterruptionEvent(
                local_session_id="stale-session-000",
                response_id="resp-current",
                kind=InterruptionEventKind.SPEECH_STARTED,
                occurred_ns=2_000,
            ),
        )

    accepted = await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-stale",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
        ),
    )
    assert accepted is False
    assert await controller.interruption_traces(session_id) == ()


@pytest.mark.asyncio
async def test_controller_retains_completed_session_interruption_traces() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    await controller.begin_realtime_response(
        session_id,
        "resp-1",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )
    await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
        ),
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    await controller.process_control_message(session_id, teardown)

    traces = await controller.interruption_traces(session_id)
    assert len(traces) == 1
    assert traces[0].response_id == "resp-1"


@pytest.mark.asyncio
async def test_server_teardown_requests_browser_stop_then_waits_for_ack() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-server-stop"]),
        ),
        session_id_factory=SequenceFactory(["local-server-stop"]),
        browser_ack_timeout_seconds=0.2,
        teardown_step_timeout_seconds=0.2,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    teardown_task = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.FAILED,
            reason=StopReason.TRANSPORT_FAILURE,
            error="transport failed",
        )
    )
    outbound = await asyncio.wait_for(
        controller.wait_for_outbound_message(session_id),
        timeout=0.1,
    )

    assert outbound == StopMessage(
        type="stop",
        session_id=session_id,
        reason=StopReason.TRANSPORT_FAILURE,
    )
    assert controller.status == "stopping"
    assert not teardown_task.done()

    closed = await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )
    report = await teardown_task
    result = await controller.wait_for_terminal_result(session_id)

    assert isinstance(closed, SessionClosedMessage)
    assert report.request.outcome is SessionOutcome.FAILED
    assert report.browser_acknowledged is True
    assert result == TerminalSessionResult(
        session_id=session_id,
        outcome=SessionOutcome.FAILED,
        error="transport failed",
    )
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_duplicate_teardown_uses_first_terminal_outcome_and_closes_once() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-duplicate-stop"])),
        session_id_factory=SequenceFactory(["local-duplicate-stop"]),
        browser_ack_timeout_seconds=0.2,
        teardown_step_timeout_seconds=0.2,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    first = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.TIMED_OUT,
            reason=StopReason.TIMEOUT,
        )
    )
    outbound = await asyncio.wait_for(controller.wait_for_outbound_message(session_id), 0.1)
    second = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        )
    )
    await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )

    first_report, second_report = await asyncio.gather(first, second)
    result = await controller.wait_for_terminal_result(session_id)

    assert isinstance(outbound, StopMessage)
    assert first_report is second_report
    assert first_report.request.outcome is SessionOutcome.TIMED_OUT
    assert result.outcome is SessionOutcome.TIMED_OUT
    assert launcher.handles[0].closed_calls == 1


@pytest.mark.asyncio
async def test_missing_browser_ack_is_bounded_and_persists_final_trace_atomically(
    tmp_path: Path,
) -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-no-ack"])),
        session_id_factory=SequenceFactory(["local-no-browser-ack"]),
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.2,
        trace_directory=tmp_path,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    report = await asyncio.wait_for(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        ),
        timeout=0.2,
    )

    assert [(failure.step, failure.kind) for failure in report.failures] == [
        ("browser_ack", "timeout")
    ]
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"
    trace_path = tmp_path / f"{session_id}.jsonl"
    assert trace_path.is_file()
    assert "teardown_requested" in trace_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(f".{trace_path.name}.*.tmp"))


@pytest.mark.asyncio
async def test_browser_close_failure_still_returns_idle_and_rearms() -> None:
    launcher = FirstCloseFailsLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-close-fails", "token-rearmed"])
        ),
        session_id_factory=SequenceFactory(["local-close-fails", "local-rearmed-02"]),
        browser_ack_timeout_seconds=0.2,
    )
    first = await controller.activate("http://127.0.0.1:8765/voice")
    assert first.session_id is not None

    teardown = asyncio.create_task(
        controller.request_teardown(
            first.session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        )
    )
    await controller.wait_for_outbound_message(first.session_id)
    await controller.process_control_message(
        first.session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=first.session_id)
        ),
    )
    report = await teardown

    assert [(failure.step, failure.kind) for failure in report.failures] == [
        ("close_browser", "error")
    ]
    assert controller.status == "idle"
    second = await controller.activate("http://127.0.0.1:8765/voice")
    assert second.status == "opened"
    assert second.session_id == "local-rearmed-02"

    await controller.close_active_session()
