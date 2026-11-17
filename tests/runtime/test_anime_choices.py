"""Controller behavior tests for ranked anime choices (feature/anime-ranked-picker)."""

from __future__ import annotations

import pytest
from test_controller import (
    DeterministicLauncher,
    RecordingMediaManager,
    RecordingYouTubeResolver,
    SequenceFactory,
)

from realtime_action_spike.anime import AnimeChoice, AnimeResolution, RankedResolution
from realtime_action_spike.runtime.controller import VoiceSessionController
from realtime_action_spike.runtime.tokens import LaunchTokenStore

CHOICES = (
    AnimeChoice(series_id="cowboy-bebop-1065", title="Cowboy Bebop"),
    AnimeChoice(
        series_id="cowboy-bebop-the-movie-1066",
        title="Cowboy Bebop: The Movie",
    ),
)


class ChoicesAnimeAdapter:
    """Ranks a choice set; resolving the exact displayed title succeeds."""

    def __init__(self, choices: tuple[AnimeChoice, ...] = CHOICES) -> None:
        self._choices = choices
        self.resolved_queries: list[str] = []

    async def resolve(self, query: str, **kwargs: object) -> AnimeResolution:
        self.resolved_queries.append(query)
        episode = int(kwargs.get("episode", 1))  # type: ignore[arg-type]
        mode = kwargs.get("audio_mode", "auto")
        resolved_mode = "sub" if mode == "auto" else str(mode)
        matched = next((c for c in CHOICES if c.title.casefold() == query.casefold()), None)
        if matched is None:
            raise AssertionError(f"unexpected resolution query: {query}")
        return AnimeResolution(
            series_id=matched.series_id,
            series_title=matched.title,
            episode=episode,
            audio_mode=resolved_mode,  # type: ignore[arg-type]
            subtitle=f"Episode {episode} · {'Subbed' if resolved_mode == 'sub' else 'Dubbed'}",
            poster_url=None,
            stream_url=f"https://hls.anidb.app/stream/example/{matched.series_id}/ep-{episode}.m3u8",
            available_episode_numbers=(1, 2),
        )

    async def resolve_or_rank(self, query: str, **kwargs: object) -> RankedResolution:
        exact = tuple(c for c in self._choices if c.title.casefold() == query.casefold())
        if len(exact) == 1:
            return RankedResolution(
                kind="resolved",
                resolution=await self.resolve(query, **kwargs),
                choices=(),
            )
        return RankedResolution(kind="choices", resolution=None, choices=self._choices)


class ResolvingAnimeAdapter(ChoicesAnimeAdapter):
    async def resolve_or_rank(self, query: str, **kwargs: object) -> RankedResolution:
        episode = int(kwargs.get("episode", 1))  # type: ignore[arg-type]
        mode = kwargs.get("audio_mode", "auto")
        resolved_mode = "sub" if mode == "auto" else str(mode)
        resolution = AnimeResolution(
            series_id="cowboy-bebop-1065",
            series_title="Cowboy Bebop",
            episode=episode,
            audio_mode=resolved_mode,  # type: ignore[arg-type]
            subtitle=f"Episode {episode} · Subbed",
            poster_url="https://cdn.xlsbox.com/poster/small/1065.jpg",
            stream_url=f"https://hls.anidb.app/stream/example/ep-{episode}.m3u8",
            available_episode_numbers=(1, 2, 3, 4),
        )
        return RankedResolution(kind="resolved", resolution=resolution, choices=())


def make_controller(adapter, pending_ids: list[str]) -> VoiceSessionController:
    media = RecordingMediaManager()
    return VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-choices"])),
        session_id_factory=SequenceFactory(["local-choices-01"]),
        pending_id_factory=SequenceFactory(pending_ids),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=RecordingYouTubeResolver(),
        anime_adapter=adapter,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_ambiguous_anime_prepares_choices_and_resolves_selected_once() -> None:
    adapter = ChoicesAnimeAdapter()
    controller = make_controller(adapter, ["pending-ch-1", "pending-ch-2", "pending-sel-1"])
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    result = await controller.prepare_anime_media(
        session_id=session_id,
        query="cowboy",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )
    outbound = await controller.wait_for_outbound_message(session_id)

    assert result["status"] == "awaiting_choice"
    assert result["choice_count"] == 2
    assert result["choices"] == [
        {"position": 1, "title": "Cowboy Bebop"},
        {"position": 2, "title": "Cowboy Bebop: The Movie"},
    ]
    assert "series_id" not in str(result)
    assert "pending_id" not in str(result)
    assert outbound.type == "media_choices"
    assert [choice.position for choice in outbound.choices] == [1, 2]
    assert outbound.choices[1].title == "Cowboy Bebop: The Movie"

    chosen = await controller.choose_anime_media(session_id=session_id, position=2)
    confirmation = await controller.wait_for_outbound_message(session_id)

    assert chosen["status"] == "awaiting_confirmation"
    assert chosen["pending_id"] == "pending-sel-1"
    assert confirmation.type == "media_confirmation"
    assert confirmation.title == "Cowboy Bebop: The Movie"
    assert adapter.resolved_queries == ["Cowboy Bebop: The Movie"]

    accepted = await controller.decide_media(
        session_id=session_id,
        pending_id="pending-sel-1",
        action="accept",
        revision=None,
    )
    assert accepted["media_handoff"] is True

    with pytest.raises(ValueError, match="no ranked anime choices"):
        await controller.choose_anime_media(session_id=session_id, position=1)


@pytest.mark.asyncio
async def test_clear_winner_resolves_directly_without_choices() -> None:
    adapter = ResolvingAnimeAdapter()
    controller = make_controller(adapter, ["pending-direct-01"])
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    result = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )
    outbound = await controller.wait_for_outbound_message(session_id)

    assert result["status"] == "awaiting_confirmation"
    assert outbound.type == "media_confirmation"
    assert adapter.resolved_queries == []


@pytest.mark.asyncio
async def test_new_prepare_supersedes_choices_and_replay_fails_closed() -> None:
    adapter = ChoicesAnimeAdapter()
    controller = make_controller(
        adapter,
        ["pending-ch-1", "pending-ch-2", "pending-ch-3", "pending-ch-4", "pending-sel-1"],
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    first = await controller.prepare_anime_media(
        session_id=session_id,
        query="cowboy",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )
    await controller.wait_for_outbound_message(session_id)
    assert first["status"] == "awaiting_choice"

    # A newer request supersedes the older choice set entirely.
    second = await controller.prepare_anime_media(
        session_id=session_id,
        query="bebop",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )
    await controller.wait_for_outbound_message(session_id)
    assert second["query"] == "bebop"

    chosen = await controller.choose_anime_media(session_id=session_id, position=1)
    await controller.wait_for_outbound_message(session_id)
    assert chosen["status"] == "awaiting_confirmation"

    # The consumed set cannot be selected from again.
    with pytest.raises(ValueError, match="no ranked anime choices"):
        await controller.choose_anime_media(session_id=session_id, position=2)


@pytest.mark.asyncio
async def test_out_of_range_position_fails_closed() -> None:
    adapter = ChoicesAnimeAdapter()
    controller = make_controller(adapter, ["pending-ch-1", "pending-ch-2"])
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    await controller.prepare_anime_media(
        session_id=session_id,
        query="cowboy",
        episode=None,
        episode_offset=None,
        audio_mode="auto",
    )

    for bad_position in (0, 3, 9):
        with pytest.raises(ValueError, match="position"):
            await controller.choose_anime_media(session_id=session_id, position=bad_position)


@pytest.mark.asyncio
async def test_choices_require_active_session() -> None:
    adapter = ChoicesAnimeAdapter()
    controller = make_controller(adapter, ["pending-ch-1", "pending-ch-2"])

    with pytest.raises(Exception, match="not from the originating session"):
        await controller.choose_anime_media(session_id="local-choices-01", position=1)
