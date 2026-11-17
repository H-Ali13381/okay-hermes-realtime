"""Bounded direct AnimeOnsen playback fallback for anime resolution."""

from __future__ import annotations

import base64
import re
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

import httpx

from .anime import AnimeFallbackStream, AnimeStreamUnavailableError, _title_fit_score

_SITE = "https://www.animeonsen.xyz"
_AUTHENTICATED_ORIGIN_HOSTS = {
    "API": frozenset({"api.animeonsen.xyz"}),
    "search": frozenset({"search.animeonsen.xyz"}),
}
_ANILIST_URL = "https://graphql.anilist.co"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ANILIST_QUERY = """
query ($title: String) {
  Media(search: $title, type: ANIME) {
    idMal
    episodes
    format
    title { english romaji native }
  }
}
"""
_META_RE = re.compile(r'<meta\b[^>]*>', re.I)


class AnimeOnsenHttpClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...

    async def post(self, url: str, **kwargs: Any) -> Any: ...


class HttpxAnimeOnsenHttpClient:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, **kwargs)


class _Session:
    def __init__(self, token: str, api_origin: str, search_origin: str, search_token: str) -> None:
        self.token = token
        self.api_origin = api_origin
        self.search_origin = search_origin
        self.search_token = search_token


class AnimeOnsenResolver:
    def __init__(self, *, client: AnimeOnsenHttpClient | None = None) -> None:
        self._client = client or HttpxAnimeOnsenHttpClient()
        self._session: _Session | None = None

    async def resolve(self, title: str, episode: int) -> AnimeFallbackStream:
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 1:
            raise AnimeStreamUnavailableError("invalid fallback episode")
        if not title or len(title) > 300:
            raise AnimeStreamUnavailableError("invalid fallback title")

        media = await self._anilist(title)
        session = await self._get_session()
        candidate = await self._select_candidate(session, media, title)
        source_number = await self._episode_source_number(session, candidate["content_id"], episode)
        content_id = quote(candidate["content_id"], safe="")
        source_id = quote(source_number, safe="")
        video = await self._api_json(session, f"/v4/content/{content_id}/video/{source_id}")
        uri = video.get("uri") if isinstance(video, dict) else None
        stream_url = uri.get("stream") if isinstance(uri, dict) else None
        if not isinstance(stream_url, str) or not stream_url:
            raise AnimeStreamUnavailableError("AnimeOnsen returned no stream URL")
        return AnimeFallbackStream(
            stream_url=stream_url,
            http_headers=(
                ("Authorization", f"Bearer {session.token}"),
                ("Referer", f"{_SITE}/"),
            ),
            source="animeonsen",
        )

    async def _get_session(self, *, force: bool = False) -> _Session:
        if self._session is not None and not force:
            return self._session
        response = await self._client.get(
            f"{_SITE}/",
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._require_ok(response, "homepage")
        cookies = response.headers.get_list("set-cookie")
        cookie = next(
            (
                match.group(1)
                for value in cookies
                if (match := re.search(r"(?:^|;\s*)ao\.session=([^;]+)", value))
            ),
            None,
        )
        if not cookie:
            raise AnimeStreamUnavailableError("AnimeOnsen session cookie missing")
        meta = self._meta_content(response.text)
        api_origin = meta.get("ao-api-origin")
        search_origin = meta.get("ao-search-origin")
        search_token = meta.get("ao-search-token")
        if not api_origin or not search_origin or not search_token:
            raise AnimeStreamUnavailableError("AnimeOnsen session bootstrap data missing")
        validated_api = self._validated_origin(api_origin, "API")
        validated_search = self._validated_origin(search_origin, "search")
        self._session = _Session(
            token=self._decode_token(cookie),
            api_origin=validated_api,
            search_origin=validated_search,
            search_token=search_token,
        )
        return self._session

    @staticmethod
    def _validated_origin(origin: str, kind: str) -> str:
        # Bootstrap HTML is untrusted: never let it choose where credentials go.
        try:
            parsed = urlsplit(origin)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in _AUTHENTICATED_ORIGIN_HOSTS[kind]
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
            )
        except ValueError:
            # URL parser errors can contain attacker-controlled input or secrets.
            raise AnimeStreamUnavailableError(f"AnimeOnsen {kind} origin is invalid") from None
        if not valid:
            raise AnimeStreamUnavailableError(f"AnimeOnsen {kind} origin is invalid")
        return f"https://{parsed.hostname}" + (":443" if parsed.port == 443 else "")

    @staticmethod
    def _decode_token(cookie: str) -> str:
        try:
            raw = base64.b64decode(unquote(cookie), validate=True)
            token = "".join(chr(byte + 1) for byte in raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AnimeStreamUnavailableError("AnimeOnsen session token is malformed") from exc
        if not token or any(not 0x20 <= ord(char) <= 0x7E for char in token) or len(token) > 2048:
            raise AnimeStreamUnavailableError("AnimeOnsen session token is invalid")
        return token

    @staticmethod
    def _meta_content(html: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for tag in _META_RE.findall(html):
            name = re.search(r'\bname=["\']([^"\']+)["\']', tag, re.I)
            content = re.search(r'\bcontent=["\']([^"\']+)["\']', tag, re.I)
            if name and content:
                result[name.group(1).lower()] = content.group(1)
        return result

    async def _anilist(self, title: str) -> dict[str, Any]:
        response = await self._client.post(
            _ANILIST_URL,
            json={"query": _ANILIST_QUERY, "variables": {"title": title}},
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        self._require_ok(response, "AniList lookup")
        payload = response.json()
        media = payload.get("data", {}).get("Media") if isinstance(payload, dict) else None
        if not isinstance(media, dict):
            raise AnimeStreamUnavailableError("AniList returned no matching media")
        return media

    async def _search(self, session: _Session, title: str) -> list[dict[str, Any]]:
        response = await self._client.post(
            f"{session.search_origin}/multi-search",
            json={"queries": [{"indexUid": "content", "q": title, "limit": 20}]},
            headers={
                "Authorization": f"Bearer {session.search_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": _SITE,
                "Referer": f"{_SITE}/",
                "User-Agent": _UA,
            },
        )
        if response.status_code in {401, 403}:
            raise AnimeStreamUnavailableError("AnimeOnsen search authorization failed")
        self._require_ok(response, "search")
        payload = response.json()
        hits = payload.get("results", [{}])[0].get("hits", []) if isinstance(payload, dict) else []
        return [item for item in hits if isinstance(item, dict)] if isinstance(hits, list) else []

    async def _select_candidate(
        self,
        session: _Session,
        media: dict[str, Any],
        title: str,
    ) -> dict[str, Any]:
        titles = [
            value
            for value in (
                (media.get("title") or {}).get("english"),
                (media.get("title") or {}).get("romaji"),
                (media.get("title") or {}).get("native"),
                title,
            )
            if isinstance(value, str) and value
        ]
        hits = await self._search(session, titles[0])
        scored: list[tuple[float, dict[str, Any], int]] = []
        for index, hit in enumerate(hits[:14]):
            hit_titles = [
                value
                for value in (
                    hit.get("content_title_en"),
                    hit.get("content_title"),
                    hit.get("content_title_jp"),
                )
                if isinstance(value, str) and value
            ]
            score = max(
                (_title_fit_score(query, value) for query in titles for value in hit_titles),
                default=0.0,
            )
            if score >= 0.7 and isinstance(hit.get("content_id"), str):
                scored.append((score, hit, index))
        scored.sort(key=lambda item: (-item[0], item[2]))
        if not scored:
            raise AnimeStreamUnavailableError("AnimeOnsen match not confident")

        expected_mal = media.get("idMal")
        examined: list[tuple[float, dict[str, Any]]] = []
        for score, hit, _index in scored:
            try:
                video = await self._api_json(
                    session,
                    f"/v4/content/{quote(hit['content_id'], safe='')}/video/1",
                )
            except AnimeStreamUnavailableError:
                continue
            metadata = video.get("metadata") if isinstance(video, dict) else None
            mal_id = metadata.get("mal_id") if isinstance(metadata, dict) else None
            if isinstance(expected_mal, int) and expected_mal > 0 and mal_id != expected_mal:
                continue
            examined.append((score, hit))
        if not examined:
            raise AnimeStreamUnavailableError("AnimeOnsen match not confident")
        best_score, best_hit = examined[0]
        runner_up = examined[1][0] if len(examined) > 1 else 0.0
        if best_score < 0.82 or (runner_up and best_score - runner_up < 0.08):
            raise AnimeStreamUnavailableError("AnimeOnsen match not confident")
        return best_hit

    async def _episode_source_number(
        self,
        session: _Session,
        content_id: str,
        episode: int,
    ) -> str:
        payload = await self._api_json(
            session,
            f"/v4/content/{quote(content_id, safe='')}/episodes",
        )
        if not isinstance(payload, dict):
            raise AnimeStreamUnavailableError("AnimeOnsen episode map is invalid")
        for key, detail in sorted(payload.items(), key=lambda item: str(item[0])):
            if not str(key).isdigit() or int(str(key)) != episode:
                continue
            source_number = detail.get("source_number") if isinstance(detail, dict) else None
            if source_number is None or source_number == "":
                source_number = key
            return str(source_number)[:40]
        raise AnimeStreamUnavailableError(f"AnimeOnsen episode {episode} not found")

    async def _api_json(
        self,
        session: _Session,
        path: str,
        *,
        retry: bool = True,
    ) -> dict[str, Any]:
        response = await self._client.get(
            f"{session.api_origin}{path}",
            headers={
                "Authorization": f"Bearer {session.token}",
                "Accept": "application/json, text/plain, */*",
                "Origin": _SITE,
                "Referer": f"{_SITE}/",
                "User-Agent": _UA,
            },
        )
        if response.status_code in {401, 403} and retry:
            await self._get_session(force=True)
            refreshed = self._session
            if refreshed is None:
                raise AnimeStreamUnavailableError("AnimeOnsen session refresh failed")
            return await self._api_json(refreshed, path, retry=False)
        self._require_ok(response, path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise AnimeStreamUnavailableError(f"AnimeOnsen {path} returned invalid JSON")
        return payload

    @staticmethod
    def _require_ok(response: Any, label: str) -> None:
        if response.status_code != 200:
            raise AnimeStreamUnavailableError(f"AnimeOnsen {label} HTTP {response.status_code}")
