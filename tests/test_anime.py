from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from realtime_action_spike.anime import (
    AnimeAmbiguityError,
    AnimeChoice,
    AnimeSourceAdapter,
    AnimeSourceError,
    CurlAnimeTransport,
    MissingAnimeLanguageError,
    parse_embed_html,
    parse_episode_json,
    parse_languages_json,
    parse_master_playlist,
    parse_search_html,
    select_best_variant,
)

FIXTURES = Path(__file__).parent / "fixtures" / "anime"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_fixture_parsers_return_typed_bounded_values() -> None:
    candidates = parse_search_html(fixture("search.html"))
    episodes = parse_episode_json(fixture("episodes.json"))
    languages = parse_languages_json(fixture("languages.json"))
    master_url = parse_embed_html(fixture("embed.html"))
    variants = parse_master_playlist(fixture("master.m3u8"), master_url)

    assert [(item.series_id, item.title) for item in candidates] == [
        ("cowboy-bebop-1065", "Cowboy Bebop"),
        ("cowboy-bebop-the-movie-1066", "Cowboy Bebop: The Movie"),
    ]
    assert candidates[0].poster_url == (
        "https://cdn.xlsbox.com/poster/small/1782735600/1065.jpg"
    )
    assert [(item.episode_id, item.number) for item in episodes] == [(54772, 1), (54773, 2)]
    assert {item.code: item.name for item in languages} == {
        "eng": "English",
        "jpn": "Japanese",
    }
    assert master_url == "https://hls.anidb.app/stream/example/master.m3u8"
    assert [(item.height, item.bandwidth) for item in variants] == [
        (1080, 1_119_823),
        (720, 655_564),
        (360, 313_788),
    ]
    assert select_best_variant(variants).url == (
        "https://hls.anidb.app/stream/example/index-1080.m3u8"
    )


def test_parsed_records_are_immutable() -> None:
    candidate = parse_search_html(fixture("search.html"))[0]

    with pytest.raises(FrozenInstanceError):
        candidate.title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_search_html, b"<html>no results</html>"),
        (parse_episode_json, b"not-json"),
        (parse_episode_json, b'{"episodes":[{"id":"bad","number":1}]}'),
        (parse_languages_json, b'{"languages":[]}'),
        (parse_embed_html, b"<html>missing stream</html>"),
    ],
)
def test_malformed_or_empty_source_payloads_raise_bounded_errors(parser, payload: bytes) -> None:
    with pytest.raises(AnimeSourceError) as caught:
        parser(payload)

    assert len(str(caught.value)) < 180
    assert payload.decode(errors="ignore") not in str(caught.value)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            b'<a href="https://evil.example/anime/a-1">'
            b'<img alt="A" src="https://cdn.xlsbox.com/poster/small/1/a.jpg"></a>',
            "search",
        ),
        (
            b'<a href="/anime/a-1"><img alt="A" src="https://evil.example/a.jpg"></a>',
            "poster",
        ),
        (
            b'{"languages":[{"code":"jpn","name":"Japanese",'
            b'"embed_url":"https://evil.example/embed/x"}]}',
            "embed",
        ),
        (
            b"<script>setup({file: 'https://evil.example/master.m3u8'})</script>",
            "stream",
        ),
    ],
)
def test_source_parsers_enforce_narrow_url_hosts(payload: bytes, message: str) -> None:
    parser = {
        "search": parse_search_html,
        "poster": parse_search_html,
        "embed": parse_languages_json,
        "stream": parse_embed_html,
    }[message]
    with pytest.raises(AnimeSourceError, match=message):
        parser(payload)


@pytest.mark.parametrize("port", ["99999", "bad"])
def test_embed_parser_normalizes_malformed_ports_to_bounded_source_error(port: str) -> None:
    payload = f"file: 'https://hls.anidb.app:{port}/stream/a.m3u8'".encode()

    with pytest.raises(AnimeSourceError, match="invalid stream URL"):
        parse_embed_html(payload)


def test_playlist_rejects_cross_host_and_control_character_variants() -> None:
    with pytest.raises(AnimeSourceError, match="playlist"):
        parse_master_playlist(
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x1\n"
            b"https://evil.example/v.m3u8\n",
            "https://hls.anidb.app/stream/example/master.m3u8",
        )
    with pytest.raises(AnimeSourceError, match="playlist"):
        parse_master_playlist(
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1x1\n"
            b"bad\x00.m3u8\n",
            "https://hls.anidb.app/stream/example/master.m3u8",
        )


class FakeCurlProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"ok",
        stderr: bytes = b"",
        returncode: int = 0,
        block: bool = False,
        stubborn: bool = False,
    ) -> None:
        self._released = asyncio.Event()
        self.stdout = FakeStream(stdout, self._released if block else None)
        self.stderr = FakeStream(stderr, self._released if block else None)
        self.returncode: int | None = None if block else returncode
        self._final_returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.stubborn = stubborn
        if not block:
            self._released.set()


    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.stubborn:
            self.returncode = -15
            self._released.set()

    def kill(self) -> None:
        self.kill_calls += 1
        if not self.stubborn:
            self.returncode = -9
            self._released.set()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._released.wait()
        assert self.returncode is not None
        return self.returncode


class FakeStream:
    def __init__(self, payload: bytes, release: asyncio.Event | None = None) -> None:
        self._payload = payload
        self._release = release
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._release is not None:
            await self._release.wait()
        if self._offset >= len(self._payload):
            return b""
        end = len(self._payload) if size < 0 else min(len(self._payload), self._offset + size)
        chunk = self._payload[self._offset : end]
        self._offset = end
        return chunk


class RecordingCurlFactory:
    def __init__(self, process: FakeCurlProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeCurlProcess:
        self.calls.append((argv, kwargs))
        return self.process


@pytest.mark.asyncio
async def test_transport_builds_fixed_argv_without_shell_or_credentials() -> None:
    factory = RecordingCurlFactory(FakeCurlProcess(stdout=b"payload"))
    transport = CurlAnimeTransport(
        curl_bin="/usr/bin/curl",
        process_factory=factory,
        timeout_seconds=2,
    )

    payload = await transport.search("Cowboy Bebop & friends")

    assert payload == b"payload"
    argv, kwargs = factory.calls[0]
    assert argv[0] == "/usr/bin/curl"
    assert "--user-agent" in argv
    assert "Mozilla/5.0" in argv[argv.index("--user-agent") + 1]
    assert argv[argv.index("--max-filesize") + 1] == "2000000"
    assert argv[-2] == "--"
    assert argv[-1] == "https://anidb.app/browse?q=Cowboy+Bebop+%26+friends"
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert "shell" not in kwargs
    assert not any("cookie" in value.casefold() for value in argv)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/embed/token",
        "http://anidb.app/embed/token",
        "https://anidb.app/embed/bad\nvalue",
        "https://user:secret@anidb.app/embed/token",
    ],
)
async def test_transport_rejects_non_allowlisted_dynamic_targets_before_spawn(target: str) -> None:
    factory = RecordingCurlFactory(FakeCurlProcess())
    transport = CurlAnimeTransport(process_factory=factory)

    with pytest.raises(AnimeSourceError, match="URL"):
        await transport.fetch_embed(target)

    assert factory.calls == []


@pytest.mark.asyncio
async def test_transport_caps_output_and_redacts_stderr() -> None:
    secret = b"signed-token-must-not-leak"
    transport = CurlAnimeTransport(
        max_output_bytes=4,
        process_factory=RecordingCurlFactory(
            FakeCurlProcess(stdout=b"12345", stderr=secret, returncode=1)
        ),
    )

    with pytest.raises(AnimeSourceError) as caught:
        await transport.search("query")

    assert secret.decode() not in str(caught.value)
    assert len(str(caught.value)) < 180


@pytest.mark.asyncio
async def test_transport_stops_and_reaps_as_soon_as_stream_exceeds_cap() -> None:
    process = FakeCurlProcess(stdout=b"", block=True)
    process.stdout = FakeStream(b"12345")
    transport = CurlAnimeTransport(
        max_output_bytes=4,
        terminate_timeout_seconds=0.1,
        process_factory=RecordingCurlFactory(process),
    )

    with pytest.raises(AnimeSourceError, match="size limit"):
        await transport.search("query")

    assert process.terminate_calls == 1
    assert process.wait_calls >= 1


@pytest.mark.asyncio
async def test_transport_timeout_terminates_and_reaps_process() -> None:
    process = FakeCurlProcess(block=True)
    transport = CurlAnimeTransport(
        timeout_seconds=0.001,
        terminate_timeout_seconds=0.1,
        process_factory=RecordingCurlFactory(process),
    )

    with pytest.raises(AnimeSourceError, match="timed out"):
        await transport.search("query")

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls >= 1


@pytest.mark.asyncio
async def test_transport_cancellation_terminates_and_reaps_process() -> None:
    process = FakeCurlProcess(block=True)
    transport = CurlAnimeTransport(
        timeout_seconds=1,
        terminate_timeout_seconds=0.1,
        process_factory=RecordingCurlFactory(process),
    )
    task = asyncio.create_task(transport.search("query"))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminate_calls == 1
    assert process.wait_calls >= 1


@pytest.mark.asyncio
async def test_transport_stubborn_process_is_killed_with_bounded_failure() -> None:
    process = FakeCurlProcess(block=True, stubborn=True)
    transport = CurlAnimeTransport(
        timeout_seconds=0.001,
        terminate_timeout_seconds=0.001,
        process_factory=RecordingCurlFactory(process),
    )

    with pytest.raises(AnimeSourceError, match="forced termination"):
        await asyncio.wait_for(transport.search("query"), timeout=0.1)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


class FixtureTransport:
    def __init__(self, *, search: bytes | None = None, languages: bytes | None = None) -> None:
        self.search_payload = search or fixture("search.html")
        self.language_payload = languages or fixture("languages.json")
        self.calls: list[tuple[str, object]] = []

    async def search(self, query: str) -> bytes:
        self.calls.append(("search", query))
        return self.search_payload

    async def episodes(self, numeric_series_id: int) -> bytes:
        self.calls.append(("episodes", numeric_series_id))
        return fixture("episodes.json")

    async def languages(self, episode_id: int) -> bytes:
        self.calls.append(("languages", episode_id))
        return self.language_payload

    async def fetch_embed(self, url: str) -> bytes:
        self.calls.append(("embed", url))
        return fixture("embed.html")

    async def fetch_playlist(self, url: str) -> bytes:
        self.calls.append(("playlist", url))
        return fixture("master.m3u8")


@pytest.mark.asyncio
async def test_adapter_resolves_exact_series_episode_and_subbed_stream_without_player() -> None:
    transport = FixtureTransport()
    adapter = AnimeSourceAdapter(transport=transport, preference_provider=lambda: "sub")

    result = await adapter.resolve("Cowboy Bebop", episode=2, audio_mode="auto")

    assert result.series_id == "cowboy-bebop-1065"
    assert result.series_title == "Cowboy Bebop"
    assert result.episode == 2
    assert result.audio_mode == "sub"
    assert result.subtitle == "Episode 2 · Subbed"
    assert result.poster_url == "https://cdn.xlsbox.com/poster/small/1782735600/1065.jpg"
    assert result.stream_url == "https://hls.anidb.app/stream/example/index-1080.m3u8"
    assert transport.calls == [
        ("search", "Cowboy Bebop"),
        ("episodes", 1065),
        ("languages", 54773),
        ("embed", "https://anidb.app/embed/japanese-token"),
        ("playlist", "https://hls.anidb.app/stream/example/master.m3u8"),
    ]


@pytest.mark.asyncio
async def test_adapter_explicit_dub_override_does_not_mutate_preference() -> None:
    writes: list[str] = []
    adapter = AnimeSourceAdapter(
        transport=FixtureTransport(),
        preference_provider=lambda: "sub",
        preference_writer=writes.append,
    )

    result = await adapter.resolve("Cowboy Bebop", episode=1, audio_mode="dub")

    assert result.audio_mode == "dub"
    assert writes == []


@pytest.mark.asyncio
async def test_adapter_returns_typed_ambiguity_instead_of_guessing() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    with pytest.raises(AnimeAmbiguityError) as caught:
        await adapter.resolve("cowboy", episode=1, audio_mode="auto")

    assert caught.value.choices == ("Cowboy Bebop", "Cowboy Bebop: The Movie")


@pytest.mark.asyncio
async def test_adapter_falls_back_to_first_episode_when_numbering_skips_request() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    result = await adapter.resolve("Cowboy Bebop", episode=99, audio_mode="auto")

    assert result.episode == 1
    assert result.available_episode_numbers == (1, 2)


@pytest.mark.asyncio
async def test_adapter_never_silently_switches_missing_language() -> None:
    jpn_only = (
        b'{"languages":[{"code":"jpn","name":"Japanese",'
        b'"embed_url":"https://anidb.app/embed/japanese-token"}]}'
    )
    adapter = AnimeSourceAdapter(
        transport=FixtureTransport(languages=jpn_only),
        preference_provider=lambda: "sub",
    )

    with pytest.raises(MissingAnimeLanguageError) as caught:
        await adapter.resolve("Cowboy Bebop", episode=1, audio_mode="dub")

    assert caught.value.requested_mode == "dub"
    assert caught.value.available_modes == ("sub",)


@pytest.mark.asyncio
async def test_adapter_types_an_endpoint_with_no_supported_language_as_unavailable() -> None:
    unsupported = (
        b'{"languages":[{"code":"spa","name":"Spanish",'
        b'"embed_url":"https://anidb.app/embed/spanish-token"}]}'
    )
    adapter = AnimeSourceAdapter(
        transport=FixtureTransport(languages=unsupported),
        preference_provider=lambda: "sub",
    )

    with pytest.raises(MissingAnimeLanguageError) as caught:
        await adapter.resolve("Cowboy Bebop", episode=1, audio_mode="auto")

    assert caught.value.requested_mode == "sub"
    assert caught.value.available_modes == ()


@pytest.mark.asyncio
async def test_resolve_candidates_ranks_exact_match_first() -> None:
    transport = FixtureTransport()
    adapter = AnimeSourceAdapter(transport=transport, preference_provider=lambda: "sub")

    choices = await adapter.resolve_candidates("Cowboy Bebop")

    assert [(item.series_id, item.title) for item in choices] == [
        ("cowboy-bebop-1065", "Cowboy Bebop"),
        ("cowboy-bebop-the-movie-1066", "Cowboy Bebop: The Movie"),
    ]
    assert transport.calls == [("search", "Cowboy Bebop")]


@pytest.mark.asyncio
async def test_resolve_candidates_partial_query_keeps_source_order_under_equal_scores() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    choices = await adapter.resolve_candidates("cowboy")

    assert [item.title for item in choices] == ["Cowboy Bebop", "Cowboy Bebop: The Movie"]


@pytest.mark.asyncio
async def test_resolve_candidates_caps_limit_at_five_and_rejects_invalid_bounds() -> None:
    many = FixtureTransport(search=(
        b"".join(
            f'<a href="/anime/series-{index}-{index}"><img alt="Series {index}" '
            f'src="https://cdn.xlsbox.com/poster/small/1/{index}.jpg"></a>'.encode()
            for index in range(1, 9)
        )
    ))
    adapter = AnimeSourceAdapter(transport=many, preference_provider=lambda: "sub")

    choices = await adapter.resolve_candidates("series")
    assert len(choices) == 5

    with pytest.raises(AnimeSourceError, match="limit"):
        await adapter.resolve_candidates("series", limit=6)
    with pytest.raises(AnimeSourceError, match="limit"):
        await adapter.resolve_candidates("series", limit=0)


def test_anime_choice_is_bounded_and_immutable() -> None:
    choice = AnimeChoice(series_id="cowboy-bebop-1065", title="Cowboy Bebop")
    assert choice.series_id == "cowboy-bebop-1065"

    with pytest.raises(FrozenInstanceError):
        choice.title = "changed"  # type: ignore[misc]

    with pytest.raises(AnimeSourceError):
        AnimeChoice(series_id="not-a-valid-id", title="X")
    with pytest.raises(AnimeSourceError):
        AnimeChoice(series_id="cowboy-bebop-1065", title="")


@pytest.mark.asyncio
async def test_resolve_or_rank_returns_resolution_for_exact_unique_match() -> None:
    adapter = AnimeSourceAdapter(
        transport=FixtureTransport(),
        preference_provider=lambda: "sub",
    )

    result = await adapter.resolve_or_rank("Cowboy Bebop", episode=2, audio_mode="auto")

    assert result.kind == "resolved"
    assert result.resolution is not None
    assert result.resolution.series_id == "cowboy-bebop-1065"
    assert result.resolution.episode == 2
    assert result.choices == ()


@pytest.mark.asyncio
async def test_resolve_or_rank_returns_choices_instead_of_raising() -> None:
    transport = FixtureTransport()
    adapter = AnimeSourceAdapter(
        transport=transport,
        preference_provider=lambda: "sub",
    )

    result = await adapter.resolve_or_rank("cowboy", episode=1, audio_mode="auto")

    assert result.kind == "choices"
    assert result.resolution is None
    assert [item.title for item in result.choices] == [
        "Cowboy Bebop",
        "Cowboy Bebop: The Movie",
    ]
    assert [call for call in transport.calls if call[0] == "search"] == [("search", "cowboy")]


@pytest.mark.asyncio
async def test_resolve_or_rank_single_candidate_resolves_directly() -> None:
    single = FixtureTransport(search=(
        b'<a href="/anime/cowboy-bebop-1065"><img alt="Cowboy Bebop" '
        b'src="https://cdn.xlsbox.com/poster/small/1782735600/1065.jpg"></a>'
    ))
    adapter = AnimeSourceAdapter(transport=single, preference_provider=lambda: "sub")

    result = await adapter.resolve_or_rank("Cowboy Bebop", episode=1, audio_mode="sub")

    assert result.kind == "resolved"
    assert result.resolution is not None
    assert result.resolution.audio_mode == "sub"


@pytest.mark.asyncio
async def test_resolve_still_raises_ambiguity_for_legacy_callers() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    with pytest.raises(AnimeAmbiguityError):
        await adapter.resolve("cowboy", episode=1, audio_mode="auto")
