from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import pytest

from realtime_action_spike.anime import AnimeSourceError
from realtime_action_spike.anime_onsen import AnimeOnsenResolver


def _session_cookie(token: str) -> str:
    shifted = "".join(chr(ord(char) - 1) for char in token)
    return quote(base64.b64encode(shifted.encode()).decode())


class FakeHeaders:
    def __init__(self, cookies: list[str]) -> None:
        self._cookies = cookies

    def get_list(self, name: str) -> list[str]:
        return self._cookies if name.lower() == "set-cookie" else []


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        payload: Any = None,
        cookies: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = FakeHeaders(cookies or [])

    def json(self) -> Any:
        return self._payload


class FakeClient:
    def __init__(self, routes: dict[str, FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.routes[url]

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.routes[url]


def _routes(*, exact_mal: bool = True) -> dict[str, FakeResponse]:
    token = "test-session-token"
    api = "https://api.example.test"
    search = "https://search.example.test"
    cookie = _session_cookie(token)
    homepage = FakeResponse(
        text=(
            '<meta name="ao-api-origin" content="https://api.example.test/v4">'
            '<meta name="ao-search-origin" content="https://search.example.test">'
            '<meta name="ao-search-token" content="search-secret">'
        ),
        cookies=[f"ao.session={cookie}; Path=/"],
    )
    return {
        "https://www.animeonsen.xyz/": homepage,
        "https://graphql.anilist.co": FakeResponse(
            payload={
                "data": {
                    "Media": {
                        "idMal": 1,
                        "episodes": 26,
                        "format": "TV",
                        "title": {
                            "english": "Cowboy Bebop",
                            "romaji": "Cowboy Bebop",
                            "native": "カウボーイビバップ",
                        },
                    }
                }
            }
        ),
        f"{search}/multi-search": FakeResponse(
            payload={
                "results": [
                    {
                        "hits": [
                            {
                                "content_id": "content-1",
                                "content_title_en": "Cowboy Bebop",
                                "content_title": "Cowboy Bebop",
                            }
                        ]
                    }
                ]
            }
        ),
        f"{api}/v4/content/content-1/video/1": FakeResponse(
            payload={
                "metadata": {
                    "mal_id": 1 if exact_mal else 999,
                    "total_episodes": 26,
                    "is_movie": False,
                }
            }
        ),
        f"{api}/v4/content/content-1/episodes": FakeResponse(
            payload={"1": {"source_number": "2", "contentTitle_episode_en": "Asteroid Blues"}}
        ),
        f"{api}/v4/content/content-1/video/2": FakeResponse(
            payload={
                "uri": {
                    "stream": "https://cdn.example.test/video/manifest.mpd",
                    "subtitles": {},
                }
            }
        ),
    }


@pytest.mark.asyncio
async def test_anime_onsen_resolver_returns_bounded_stream_with_auth_headers() -> None:
    client = FakeClient(_routes())
    resolver = AnimeOnsenResolver(client=client)  # type: ignore[arg-type]

    stream = await resolver.resolve("Cowboy Bebop", 1)

    assert stream.stream_url == "https://cdn.example.test/video/manifest.mpd"
    assert stream.http_headers == (
        ("Authorization", "Bearer test-session-token"),
        ("Referer", "https://www.animeonsen.xyz/"),
    )
    assert stream.source == "animeonsen"
    anilist_calls = [call for call in client.calls if call[1] == "https://graphql.anilist.co"]
    assert anilist_calls and anilist_calls[0][0] == "POST"
    assert any(call[1].endswith("/v4/content/content-1/video/2") for call in client.calls)


@pytest.mark.asyncio
async def test_anime_onsen_resolver_rejects_low_confidence_match() -> None:
    client = FakeClient(_routes(exact_mal=False))
    resolver = AnimeOnsenResolver(client=client)  # type: ignore[arg-type]

    with pytest.raises(AnimeSourceError, match="confident"):
        await resolver.resolve("Cowboy Bebop", 1)
