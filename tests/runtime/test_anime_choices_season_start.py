"""Regression test: choosing a series whose episodes don't start at 1 (e.g. Frieren S2, ep 29)."""

from __future__ import annotations

import pytest
from test_anime_choices import make_controller

from realtime_action_spike.anime import AnimeChoice


class SeasonTwoAdapter:
    """Simulates AniDB: one candidate whose episode list starts at 29."""

    async def resolve(self, query: str, **kwargs: object):
        from realtime_action_spike.anime import AnimeResolution

        episode = int(kwargs.get("episode", 1))  # type: ignore[arg-type]
        # Mirror the real adapter: unavailable requested episode falls back to the
        # first available episode (29) instead of raising.
        if episode < 29:
            episode = 29
        return AnimeResolution(
            series_id="frieren-s2-1665",
            series_title="Frieren: Beyond Journey's End Season 2",
            episode=episode,
            audio_mode="sub",
            subtitle=f"Episode {episode} · Subbed",
            poster_url=None,
            stream_url=f"https://hls.anidb.app/stream/example/ep-{episode}.m3u8",
            available_episode_numbers=tuple(range(29, 40)),
        )

    async def resolve_or_rank(self, query: str, **kwargs: object):
        if "season 2" in query.casefold():
            # exact single-candidate path
            resolution = await self.resolve(query, **kwargs)
            from realtime_action_spike.anime import RankedResolution

            return RankedResolution(kind="resolved", resolution=resolution, choices=())
        choices = (
            AnimeChoice(series_id="frieren-1663", title="Frieren: Beyond Journey's End"),
            AnimeChoice(series_id="frieren-s2-1665", title="Frieren: Beyond Journey's End Season 2"),
        )
        from realtime_action_spike.anime import RankedResolution

        return RankedResolution(kind="choices", resolution=None, choices=choices)


@pytest.mark.asyncio
async def test_choice_for_series_not_starting_at_episode_one_resolves() -> None:
    adapter = SeasonTwoAdapter()
    controller = make_controller(adapter, ["pending-ch-1", "pending-ch-2", "pending-sel-9"])
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    result = await controller.prepare_anime_media(
        session_id=session_id, query="frieren", episode=None,
        episode_offset=None, audio_mode="auto",
    )
    assert result["status"] == "awaiting_choice"
    await controller.wait_for_outbound_message(session_id)

    chosen = await controller.choose_anime_media(session_id=session_id, position=2)
    confirmation = await controller.wait_for_outbound_message(session_id)

    assert chosen["status"] == "awaiting_confirmation"
    assert confirmation.type == "media_confirmation"
    assert confirmation.episode.episode_number == 29
