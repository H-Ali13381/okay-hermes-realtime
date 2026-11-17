from __future__ import annotations

import pytest

from realtime_action_spike.media_catalog import (
    DirectStreamLocator,
    PendingEpisodeMetadata,
    PendingMedia,
    PendingMediaCatalog,
    PendingMediaCatalogError,
    PendingResumeMetadata,
    StalePendingMediaError,
    YouTubePlaybackLocator,
    build_youtube_thumbnail_url,
)


def _youtube_pending(
    *,
    pending_id: str = "pending-abc12345",
    session_id: str = "session-abc12345",
    thumbnail_url: str | None = None,
) -> PendingMedia:
    del session_id
    return PendingMedia(
        pending_id=pending_id,
        source="youtube",
        title="Selected title",
        subtitle="Useful Channel",
        thumbnail_url=thumbnail_url,
        playback_locator=YouTubePlaybackLocator(
            video_id="dQw4w9WgXcQ",
            canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        prompt="Play this video?",
    )


def _anime_pending(
    *,
    pending_id: str = "pending-def67890",
    thumbnail_url: str | None = None,
) -> PendingMedia:
    return PendingMedia(
        pending_id=pending_id,
        source="anime",
        title="Episode three",
        subtitle="Season one · dub",
        thumbnail_url=thumbnail_url,
        playback_locator=DirectStreamLocator(
            stream_url="https://stream.example.test/anime/episode-3.m3u8",
            webpage_url="https://watch.example.test/anime/episode-3",
        ),
        episode=PendingEpisodeMetadata(
            series_title="Example Show",
            season_number=1,
            episode_number=3,
            episode_title="The third episode",
        ),
        audio_mode="dub",
        resume=PendingResumeMetadata(
            start_seconds=93,
            duration_seconds=1800,
            reason="resuming after a prior watch",
        ),
        prompt="Play episode three dubbed?",
    )


def test_youtube_thumbnail_is_derived_and_invalid_values_degrade_to_none() -> None:
    assert (
        build_youtube_thumbnail_url("dQw4w9WgXcQ")
        == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    )
    assert build_youtube_thumbnail_url("dQw4w9WgXcQ", "http://example.com/thumb.jpg") is None
    assert (
        build_youtube_thumbnail_url(
            "dQw4w9WgXcQ",
            "https://user:pass@i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        )
        is None
    )
    assert (
        build_youtube_thumbnail_url(
            "dQw4w9WgXcQ",
            "https://example.com/thumb.jpg#fragment",
        )
        is None
    )


def test_pending_media_display_projection_hides_private_locator_and_urls() -> None:
    pending = _anime_pending(
        thumbnail_url="https://images.example.test/cover.jpg",
    )

    display = pending.display_projection()

    assert display["pending_id"] == "pending-def67890"
    assert display["source"] == "anime"
    assert display["title"] == "Episode three"
    assert display["subtitle"] == "Season one · dub"
    assert display["thumbnail_url"] == "https://images.example.test/cover.jpg"
    assert display["prompt"] == "Play episode three dubbed?"
    assert display["audio_mode"] == "dub"
    assert display["episode"]["episode_number"] == 3
    assert display["resume"]["start_seconds"] == 93
    assert "playback_locator" not in display
    assert "stream_url" not in display
    assert "webpage_url" not in display
    assert "request_query" not in display
    assert "canonical_series_id" not in display
    assert "available_episode_numbers" not in display


def test_catalog_replaces_exact_generation_and_consumes_only_once() -> None:
    catalog = PendingMediaCatalog()
    first = _youtube_pending(pending_id="pending-abc12345")
    second = _youtube_pending(pending_id="pending-xyz67890")

    catalog.create_or_replace("session-abc12345", first)
    catalog.create_or_replace("session-abc12345", second)

    with pytest.raises(StalePendingMediaError):
        catalog.get_exact("session-abc12345", first.pending_id)

    current = catalog.get_exact("session-abc12345", second.pending_id)
    assert current.pending_id == second.pending_id

    taken = catalog.take_exact("session-abc12345", second.pending_id)
    assert taken.pending_id == second.pending_id

    with pytest.raises(StalePendingMediaError):
        catalog.take_exact("session-abc12345", second.pending_id)


@pytest.mark.parametrize(
    ("session_id", "pending_id"),
    [
        ("wrong-session", "pending-abc12345"),
        ("session-abc12345", "wrong-pending"),
    ],
)
def test_catalog_clear_exact_rejects_stale_session_or_id(
    session_id: str,
    pending_id: str,
) -> None:
    catalog = PendingMediaCatalog()
    catalog.create_or_replace("session-abc12345", _youtube_pending())

    with pytest.raises(StalePendingMediaError):
        catalog.clear_exact(session_id, pending_id)


def test_catalog_clear_exact_removes_current_generation() -> None:
    catalog = PendingMediaCatalog()
    pending = _youtube_pending()
    catalog.create_or_replace("session-abc12345", pending)

    catalog.clear_exact("session-abc12345", pending.pending_id)

    with pytest.raises(StalePendingMediaError):
        catalog.get_exact("session-abc12345", pending.pending_id)


def test_catalog_rejects_invalid_ids_and_non_https_thumbnails() -> None:
    with pytest.raises(PendingMediaCatalogError):
        PendingMedia(
            pending_id="bad",
            source="youtube",
            title="Title",
            subtitle="Subtitle",
            thumbnail_url="http://example.com/not-https.jpg",
            playback_locator=YouTubePlaybackLocator(
                video_id="dQw4w9WgXcQ",
                canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
            prompt="Confirm?",
        )

    with pytest.raises(PendingMediaCatalogError):
        PendingMediaCatalog().create_or_replace("bad", _youtube_pending())


def test_direct_stream_locator_carries_bounded_http_headers() -> None:
    locator = DirectStreamLocator(
        stream_url="https://cdn.example.test/video/manifest.mpd",
        http_headers=(
            ("Authorization", "Bearer token-value"),
            ("Referer", "https://www.example.test/"),
        ),
    )

    assert locator.http_headers == (
        ("Authorization", "Bearer token-value"),
        ("Referer", "https://www.example.test/"),
    )


def test_direct_stream_locator_rejects_unsafe_http_headers() -> None:
    with pytest.raises(PendingMediaCatalogError, match="http header"):
        DirectStreamLocator(
            stream_url="https://cdn.example.test/video/manifest.mpd",
            http_headers=(("Referer", "https://example.test/\r\nX-Evil: 1"),),
        )
