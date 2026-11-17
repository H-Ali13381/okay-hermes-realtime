"""Cross-language contract test: controller output must survive the browser parser."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_controller import (
    DeterministicLauncher,
    RecordingMediaManager,
    RecordingYouTubeResolver,
    SequenceFactory,
)

from realtime_action_spike.anime import AnimeResolution, RankedResolution
from realtime_action_spike.runtime.controller import VoiceSessionController
from realtime_action_spike.runtime.tokens import LaunchTokenStore

POSTER_URL = "https://cdn.anipixcdn.co/thumbnail/1782735600/1065.jpg"


class HianimePosterAdapter:
    async def resolve(self, query: str, **kwargs: object) -> AnimeResolution:
        del query
        episode = int(kwargs.get("episode", 1))  # type: ignore[arg-type]
        return AnimeResolution(
            series_id="cowboy-bebop-1065",
            series_title="Cowboy Bebop",
            episode=episode,
            audio_mode="sub",
            subtitle=f"Episode {episode} · Subbed",
            poster_url=POSTER_URL,
            stream_url="https://cdn.animeonsen.xyz/video/manifest.mpd",
            http_headers=(("Authorization", "Bearer token-value"),),
            available_episode_numbers=(1, 2),
        )

    async def resolve_or_rank(self, query: str, **kwargs: object) -> RankedResolution:
        return RankedResolution(
            kind="resolved",
            resolution=await self.resolve(query, **kwargs),
            choices=(),
        )


@pytest.mark.asyncio
async def test_controller_confirmation_is_accepted_by_browser_parser(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "node is required for the cross-language contract test"
    root = Path(__file__).resolve().parents[2]
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-roundtrip"])),
        session_id_factory=SequenceFactory(["local-roundtrip-01"]),
        pending_id_factory=SequenceFactory(["pending-roundtrip-1"]),
        media_manager=RecordingMediaManager(),  # type: ignore[arg-type]
        youtube_resolver=RecordingYouTubeResolver(),
        anime_adapter=HianimePosterAdapter(),  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    result = await controller.prepare_anime_media(
        session_id=session_id,
        query="Cowboy Bebop",
        episode=1,
        episode_offset=None,
        audio_mode="sub",
    )
    outbound = await controller.wait_for_outbound_message(session_id)
    assert result["status"] == "awaiting_confirmation"
    assert outbound.type == "media_confirmation"
    assert outbound.thumbnail_url == POSTER_URL

    payload_path = tmp_path / "confirmation.json"
    payload_path.write_text(outbound.model_dump_json(), encoding="utf-8")
    parsed = subprocess.run(
        [node, str(root / "tests/web/parse-media-confirmation.mjs"), str(payload_path)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    assert parsed.stdout.strip() == POSTER_URL
