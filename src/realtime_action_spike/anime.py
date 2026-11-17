"""Bounded native anime source resolution derived from ani-cli's public source flow."""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal, Protocol
from urllib.parse import quote_plus, urljoin, urlsplit

_ANIDB_ORIGIN = "https://anidb.app"
_ANIDB_HOST = urlsplit(_ANIDB_ORIGIN).hostname
assert _ANIDB_HOST is not None
_HIANIME_ORIGIN = "https://hianime.fm"
_HIANIME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_POSTER_PATH_PREFIXES = {
    "cdn.xlsbox.com": "/poster/",
    "cdn.anipixcdn.co": "/thumbnail/",
}
_EMBED_HOSTS = {"anidb.app", "zokoanime.video", "megaplay.buzz"}
_EMBED_PATH_PREFIXES = {
    "anidb.app": "/embed/",
    "zokoanime.video": "/stream/",
    "megaplay.buzz": "/stream/",
}
_STREAM_HOST_SUFFIXES = ("hls.anidb.app", "anipixcdn.co", "megaplay.buzz", "zokoanime.video")
_STREAM_HOST = "hls.anidb.app"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)
_SERIES_ID_RE = re.compile(r"^[a-z0-9-]+-[1-9][0-9]*$")
_STREAM_RE = re.compile(r"\bfile\s*:\s*(['\"])(https://[^'\"]+)\1")
_RESOLUTION_RE = re.compile(r"(?:^|,)RESOLUTION=([1-9][0-9]*)x([1-9][0-9]*)(?:,|$)")
_BANDWIDTH_RE = re.compile(r"(?:^|,)BANDWIDTH=([1-9][0-9]*)(?:,|$)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_CANDIDATES = 20
_MAX_EPISODES = 2_000
_MAX_LANGUAGES = 20
_MAX_VARIANTS = 20


class AnimeSourceError(RuntimeError):
    """The bounded anime source could not produce a safe playable result."""


class AnimeSourceUnavailableError(AnimeSourceError):
    """The external anime catalog is unavailable; retrying the query will not help."""


class AnimeAmbiguityError(AnimeSourceError):
    def __init__(self, choices: tuple[str, ...]) -> None:
        self.choices = choices
        super().__init__("anime title is ambiguous; choose one of the returned titles")


class MissingAnimeEpisodeError(AnimeSourceError):
    def __init__(self, episode: int) -> None:
        self.episode = episode
        super().__init__(f"episode {episode} is not available")


class MissingAnimeLanguageError(AnimeSourceError):
    def __init__(
        self,
        requested_mode: Literal["sub", "dub"],
        available_modes: tuple[Literal["sub", "dub"], ...],
    ) -> None:
        self.requested_mode = requested_mode
        self.available_modes = available_modes
        super().__init__(f"requested {requested_mode} audio is not available")


@dataclass(frozen=True, slots=True)
class AnimeCandidate:
    series_id: str
    title: str
    poster_url: str | None

    @property
    def numeric_id(self) -> int:
        return int(self.series_id.rsplit("-", 1)[1])


@dataclass(frozen=True, slots=True)
class AnimeEpisode:
    episode_id: int
    number: int


@dataclass(frozen=True, slots=True)
class AnimeLanguage:
    code: Literal["eng", "jpn"]
    name: str
    embed_url: str


@dataclass(frozen=True, slots=True)
class HlsVariant:
    url: str
    width: int
    height: int
    bandwidth: int


@dataclass(frozen=True, slots=True)
class AnimeResolution:
    series_id: str
    series_title: str
    episode: int
    audio_mode: Literal["sub", "dub"]
    subtitle: str
    poster_url: str | None
    stream_url: str
    available_episode_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AnimeChoice:
    """One bounded display-only search candidate; never carries a stream locator."""

    series_id: str
    title: str

    def __post_init__(self) -> None:
        if not _SERIES_ID_RE.fullmatch(self.series_id):
            raise AnimeSourceError("invalid anime series id")
        if (
            not isinstance(self.title, str)
            or not self.title
            or len(self.title) > 300
            or _CONTROL_RE.search(self.title)
        ):
            raise AnimeSourceError("invalid anime title")


@dataclass(frozen=True, slots=True)
class RankedResolution:
    kind: Literal["resolved", "choices"]
    resolution: AnimeResolution | None
    choices: tuple[AnimeChoice, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"resolved", "choices"}:
            raise AnimeSourceError("invalid ranked resolution kind")
        if self.kind == "resolved":
            if not isinstance(self.resolution, AnimeResolution) or self.choices:
                raise AnimeSourceError("invalid resolved ranked outcome")
            return
        if self.resolution is not None or not self.choices:
            raise AnimeSourceError("invalid choices ranked outcome")


def _normalized_tokens(value: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    pieces = (
        char if char.isalnum() else " "
        for char in decomposed
        if not unicodedata.combining(char)
    )
    return tuple("".join(pieces).split())


def _contains_contiguous_span(
    haystack: tuple[str, ...],
    needle: tuple[str, ...],
) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    window = len(needle)
    for index in range(len(haystack) - window + 1):
        if haystack[index : index + window] == needle:
            return True
    return False


_EXACT_SCORE = 1.0
_CONTAINMENT_BASE = 0.92
_CONTAINMENT_SPAN = 0.06
_JACCARD_SCALE = 0.78


def _title_fit_score(query: str, candidate_title: str) -> float:
    """Fixed deterministic fit score: exact match, then containment, then token overlap."""
    query_tokens = _normalized_tokens(query)
    candidate_tokens = _normalized_tokens(candidate_title)
    if not query_tokens or not candidate_tokens:
        return 0.0
    if query_tokens == candidate_tokens:
        return _EXACT_SCORE
    if _contains_contiguous_span(candidate_tokens, query_tokens):
        coverage = len(query_tokens) / len(candidate_tokens)
        return min(_CONTAINMENT_BASE + _CONTAINMENT_SPAN * coverage, 0.999999)
    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)
    union = len(query_set | candidate_set)
    if not union:
        return 0.0
    return round(_JACCARD_SCALE * (len(query_set & candidate_set) / union), 6)


_CLEAR_WIN_MARGIN = 0.15
_MAX_CHOICES = 5


def _rank_candidates(
    query: str,
    candidates: Sequence[AnimeCandidate],
    limit: int = _MAX_CHOICES,
) -> tuple[AnimeChoice, ...]:
    """Rank already-fetched search results; never performs another source request."""
    scored = [
        (_title_fit_score(query, item.title), index, item)
        for index, item in enumerate(candidates)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return tuple(
        AnimeChoice(series_id=item.series_id, title=item.title)
        for _score, _index, item in scored[:limit]
    )


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_series_id: str | None = None
        self.results: list[AnimeCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a":
            href = values.get("href") or ""
            if href.startswith("https://"):
                parsed = urlsplit(href)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != _ANIDB_HOST
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port not in {None, 443}
                    or parsed.query
                    or parsed.fragment
                ):
                    self.current_series_id = None
                    return
                href = parsed.path
            match = re.fullmatch(r"/anime/([a-z0-9-]+-[1-9][0-9]*)", href)
            self.current_series_id = match.group(1) if match else None
            return
        if tag != "img" or self.current_series_id is None:
            return
        title = (values.get("alt") or "").strip()
        if not title or len(title) > 300 or _CONTROL_RE.search(title):
            return
        raw_poster = (values.get("src") or "").strip()
        poster_url = _validate_poster_url(raw_poster)
        self.results.append(
            AnimeCandidate(
                series_id=self.current_series_id,
                title=title,
                poster_url=poster_url,
            )
        )
        self.current_series_id = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.current_series_id = None


def _decode(payload: bytes, label: str) -> str:
    if not payload:
        raise AnimeSourceError(f"{label} response was empty")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnimeSourceError(f"{label} response was not UTF-8") from exc


def _load_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(_decode(payload, label))
    except json.JSONDecodeError as exc:
        raise AnimeSourceError(f"{label} response was malformed") from exc
    if not isinstance(parsed, dict):
        raise AnimeSourceError(f"{label} response had an invalid shape")
    return parsed


def _validate_url(
    value: str,
    *,
    hosts: set[str],
    path_prefix: str,
    label: str,
) -> str:
    if not value or len(value) > 2_048 or _CONTROL_RE.search(value):
        raise AnimeSourceError(f"invalid {label}")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AnimeSourceError(f"invalid {label}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.startswith(path_prefix)
    ):
        raise AnimeSourceError(f"invalid {label}")
    return value


def _validate_poster_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    prefix = _POSTER_PATH_PREFIXES.get(host)
    if prefix is None:
        raise AnimeSourceError("invalid poster URL")
    return _validate_url(value, hosts={host}, path_prefix=prefix, label="poster URL")


def _validate_embed_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    prefix = _EMBED_PATH_PREFIXES.get(host)
    if prefix is None:
        raise AnimeSourceError("invalid embed URL")
    return _validate_url(value, hosts={host}, path_prefix=prefix, label="embed URL")


def _validate_stream_url(value: str, *, label: str = "stream URL") -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _STREAM_HOST_SUFFIXES):
        raise AnimeSourceError(f"invalid {label}")
    return _validate_url(value, hosts={host}, path_prefix="/", label=label)


def parse_search_html(payload: bytes) -> tuple[AnimeCandidate, ...]:
    parser = _SearchParser()
    try:
        parser.feed(_decode(payload, "anime search"))
        parser.close()
    except AnimeSourceError:
        raise
    except Exception as exc:
        raise AnimeSourceError("anime search response was malformed") from exc
    if not parser.results:
        raise AnimeSourceUnavailableError("anime catalog returned no search results")
    unique: dict[str, AnimeCandidate] = {}
    for candidate in parser.results:
        if _SERIES_ID_RE.fullmatch(candidate.series_id):
            unique.setdefault(candidate.series_id, candidate)
        if len(unique) >= _MAX_CANDIDATES:
            break
    if not unique:
        raise AnimeSourceUnavailableError("anime catalog returned no valid results")
    return tuple(unique.values())


def parse_episode_json(payload: bytes) -> tuple[AnimeEpisode, ...]:
    entries = _load_json(payload, "episode list").get("episodes")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_EPISODES:
        raise AnimeSourceError("episode list response had an invalid shape")
    result: list[AnimeEpisode] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise AnimeSourceError("episode list response had an invalid entry")
        episode_id = entry.get("id")
        number = entry.get("number")
        if (
            isinstance(episode_id, bool)
            or not isinstance(episode_id, int)
            or episode_id < 1
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
        ):
            raise AnimeSourceError("episode list response had an invalid entry")
        result.append(AnimeEpisode(episode_id=episode_id, number=number))
    return tuple(result)


def parse_languages_json(payload: bytes) -> tuple[AnimeLanguage, ...]:
    entries = _load_json(payload, "language list").get("languages")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_LANGUAGES:
        raise AnimeSourceError("language list response had an invalid shape")
    result: list[AnimeLanguage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise AnimeSourceError("language list response had an invalid entry")
        code = entry.get("code")
        name = entry.get("name")
        embed_url = entry.get("embed_url")
        if code not in {"eng", "jpn"}:
            continue
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise AnimeSourceError("language list response had an invalid entry")
        if not isinstance(embed_url, str):
            raise AnimeSourceError("language list response had an invalid embed URL")
        result.append(
            AnimeLanguage(
                code=code,
                name=name.strip(),
                embed_url=_validate_embed_url(embed_url),
            )
        )
    return tuple(result)


def parse_embed_html(payload: bytes) -> str:
    match = _STREAM_RE.search(_decode(payload, "embed page"))
    if match is None:
        raise AnimeSourceError("embed page did not contain a stream URL")
    return _validate_stream_url(match.group(2))


def parse_master_playlist(payload: bytes, master_url: str) -> tuple[HlsVariant, ...]:
    _validate_stream_url(master_url, label="playlist URL")
    lines = [line.strip() for line in _decode(payload, "playlist").splitlines()]
    variants: list[HlsVariant] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        if index + 1 >= len(lines):
            raise AnimeSourceError("playlist response had an invalid variant")
        attributes = line.split(":", 1)[1]
        resolution = _RESOLUTION_RE.search(attributes)
        bandwidth = _BANDWIDTH_RE.search(attributes)
        if resolution is None or bandwidth is None:
            raise AnimeSourceError("playlist response had an invalid variant")
        relative_url = lines[index + 1]
        if not relative_url or relative_url.startswith("#") or _CONTROL_RE.search(relative_url):
            raise AnimeSourceError("playlist response had an invalid variant URL")
        url = urljoin(master_url, relative_url)
        parsed = urlsplit(url)
        master = urlsplit(master_url)
        if parsed.hostname != master.hostname:
            raise AnimeSourceError("playlist response used a non-allowlisted URL")
        variants.append(
            HlsVariant(
                url=_validate_stream_url(url, label="playlist variant URL"),
                width=int(resolution.group(1)),
                height=int(resolution.group(2)),
                bandwidth=int(bandwidth.group(1)),
            )
        )
        if len(variants) > _MAX_VARIANTS:
            raise AnimeSourceError("playlist response contained too many variants")
    if not variants:
        raise AnimeSourceError("playlist response contained no playable variants")
    return tuple(variants)


def select_best_variant(variants: Sequence[HlsVariant]) -> HlsVariant:
    if not variants:
        raise AnimeSourceError("playlist contained no variants")
    return max(variants, key=lambda item: (item.height, item.bandwidth))


class _Process(Protocol):
    returncode: int | None
    stdout: Any
    stderr: Any

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Any]


class CurlAnimeTransport:
    """Fetch only fixed aniDB source endpoints through argv-only curl."""

    def __init__(
        self,
        *,
        curl_bin: str = "curl",
        timeout_seconds: float = 10.0,
        terminate_timeout_seconds: float = 1.0,
        max_output_bytes: int = 2_000_000,
        user_agent: str = _USER_AGENT,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if timeout_seconds <= 0 or terminate_timeout_seconds <= 0 or max_output_bytes < 1:
            raise ValueError("transport bounds must be positive")
        self._curl_bin = curl_bin
        self._timeout_seconds = timeout_seconds
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._user_agent = user_agent
        self._process_factory = process_factory

    async def search(self, query: str) -> bytes:
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 300 or _CONTROL_RE.search(normalized):
            raise AnimeSourceError("invalid anime search query")
        return await self._get(f"{_ANIDB_ORIGIN}/browse?q={quote_plus(normalized)}")

    async def episodes(self, numeric_series_id: int) -> bytes:
        if isinstance(numeric_series_id, bool) or numeric_series_id < 1:
            raise AnimeSourceError("invalid anime series id")
        return await self._get(f"{_ANIDB_ORIGIN}/api/frontend/anime/{numeric_series_id}/episodes")

    async def languages(self, episode_id: int) -> bytes:
        if isinstance(episode_id, bool) or episode_id < 1:
            raise AnimeSourceError("invalid anime episode id")
        return await self._get(f"{_ANIDB_ORIGIN}/api/frontend/episode/{episode_id}/languages")

    async def fetch_embed(self, url: str) -> bytes:
        return await self._get(
            _validate_url(
                url,
                hosts={"anidb.app"},
                path_prefix="/embed/",
                label="URL",
            )
        )

    async def fetch_playlist(self, url: str) -> bytes:
        return await self._get(
            _validate_url(
                url,
                hosts={_STREAM_HOST},
                path_prefix="/stream/",
                label="URL",
            )
        )

    async def _get(
        self,
        url: str,
        *,
        headers: tuple[str, ...] = (),
    ) -> bytes:
        process: _Process = await self._process_factory(
            self._curl_bin,
            "--silent",
            "--show-error",
            "--location",
            "--fail",
            "--max-filesize",
            str(self._max_output_bytes),
            "--user-agent",
            self._user_agent,
            *headers,
            "--",
            url,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr, _returncode = await asyncio.wait_for(
                asyncio.gather(
                    self._read_stream(process.stdout, self._max_output_bytes),
                    self._read_stream(process.stderr, 8_192),
                    process.wait(),
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            await self._stop_process(process)
            raise AnimeSourceUnavailableError("anime source request timed out") from exc
        except asyncio.CancelledError:
            await self._stop_process(process)
            raise
        except AnimeSourceError:
            await self._stop_process(process)
            raise
        if process.returncode != 0:
            raise AnimeSourceUnavailableError("anime source request failed")
        return stdout

    @staticmethod
    async def _read_stream(stream: Any, limit: int) -> bytes:
        captured = bytearray()
        while True:
            chunk = await stream.read(min(65_536, limit + 1 - len(captured)))
            if not chunk:
                return bytes(captured)
            captured.extend(chunk)
            if len(captured) > limit:
                raise AnimeSourceError("anime source response exceeded the size limit")

    async def _stop_process(self, process: _Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_timeout_seconds)
            return
        except TimeoutError:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_timeout_seconds)
        except TimeoutError as exc:
            raise AnimeSourceError("anime resolver resisted forced termination") from exc


_HIANIME_SEARCH_LINK_RE = re.compile(
    r'href="https://hianime\.(?:fm|at)/([a-z0-9-]+-[1-9][0-9]*)"\s+title="([^"]+)"'
)
_HIANIME_SEARCH_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"[^>]+alt="([^"]+)"', re.S)
_HIANIME_SERVER_RE = re.compile(
    r'data-type="(sub|dub)"\s+data-server-name="([^"]+)"\s+data-hash="([^"]+)"',
    re.S,
)
_EMBED_XOR_KEY = b"otaku-embed-v1"


def parse_hianime_search_html(payload: bytes) -> tuple[AnimeCandidate, ...]:
    """Translate current HiAnime search markup into bounded source candidates."""
    text = _decode(payload, "anime search")
    text = text.split('id="main-sidebar"', 1)[0]
    result: list[AnimeCandidate] = []
    for block in text.split('<div class="flw-item')[1:]:
        link = _HIANIME_SEARCH_LINK_RE.search(block)
        image = _HIANIME_SEARCH_IMG_RE.search(block)
        if link is None or image is None:
            continue
        title = html_lib.unescape(link.group(2)).strip()
        poster = html_lib.unescape(image.group(1)).strip()
        if not title or len(title) > 300 or _CONTROL_RE.search(title):
            continue
        try:
            poster_url = _validate_poster_url(poster)
        except AnimeSourceError:
            continue
        result.append(AnimeCandidate(link.group(1), title, poster_url))
        if len(result) >= _MAX_CANDIDATES:
            break
    if not result:
        raise AnimeSourceUnavailableError("anime catalog returned no search results")
    return tuple(result)


def _deobfuscate_embed_blob(blob: str) -> dict[str, object]:
    if not blob or len(blob) > 100_000:
        raise AnimeSourceUnavailableError("embed page contained no usable player config")
    try:
        raw = base64.b64decode(blob, validate=True)
        decoded = bytes(
            byte ^ _EMBED_XOR_KEY[index % len(_EMBED_XOR_KEY)]
            for index, byte in enumerate(raw)
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AnimeSourceUnavailableError("embed player config was malformed") from exc
    if not isinstance(payload, dict):
        raise AnimeSourceUnavailableError("embed player config had an invalid shape")
    return payload


class HianimeTransport(CurlAnimeTransport):
    """Current ani-cli source adapter: hianime search/servers translated to the bounded contract."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(user_agent=_HIANIME_UA, timeout_seconds=15.0, **kwargs)  # type: ignore[arg-type]

    async def search(self, query: str) -> bytes:
        normalized = self._normalize_query(query)
        page = await self._get(f"{_HIANIME_ORIGIN}/search?keyword={quote_plus(normalized)}")
        candidates = parse_hianime_search_html(page)
        return "".join(
            '<a href="/anime/{series}"><img alt="{title}" src="{poster}"></a>'.format(
                series=html_lib.escape(item.series_id, quote=True),
                title=html_lib.escape(item.title, quote=True),
                poster=html_lib.escape(item.poster_url or "", quote=True),
            )
            for item in candidates
        ).encode("utf-8")

    async def episodes(self, numeric_series_id: int) -> bytes:
        if isinstance(numeric_series_id, bool) or numeric_series_id < 1:
            raise AnimeSourceError("invalid anime series id")
        payload = await self._get(f"{_HIANIME_ORIGIN}/api/theme/episode/list/{numeric_series_id}")
        document = self._load_object(payload, "episode list")
        page = document.get("html")
        if not isinstance(page, str):
            raise AnimeSourceUnavailableError("episode list was unavailable")
        episodes: dict[int, int] = {}
        for number, episode_id in re.findall(
            r'data-number="([1-9][0-9]*)"\s+data-id="([1-9][0-9]*)"',
            page,
        ):
            episodes[int(number)] = int(episode_id)
            if len(episodes) >= _MAX_EPISODES:
                break
        if not episodes:
            raise AnimeSourceUnavailableError("episode list was unavailable")
        return json.dumps(
            {
                "episodes": [
                    {"id": episode_id, "number": number}
                    for number, episode_id in sorted(episodes.items())
                ]
            }
        ).encode("utf-8")

    async def languages(self, episode_id: int) -> bytes:
        if isinstance(episode_id, bool) or episode_id < 1:
            raise AnimeSourceError("invalid anime episode id")
        payload = await self._get(
            f"{_HIANIME_ORIGIN}/api/theme/episode/servers?episodeId={episode_id}"
        )
        document = self._load_object(payload, "server list")
        page = document.get("html")
        if not isinstance(page, str):
            raise AnimeSourceUnavailableError("server list was unavailable")
        languages: dict[str, dict[str, object]] = {}
        matches = list(_HIANIME_SERVER_RE.finditer(page))
        matches.sort(key=lambda match: 0 if match.group(2) == "HD-1" else 1)
        for match in matches:
            kind, server, encoded = match.groups()
            try:
                decoded_hash = base64.b64decode(encoded, validate=True).decode("utf-8")
                embed_url = _validate_embed_url(decoded_hash)
            except (ValueError, UnicodeDecodeError, AnimeSourceError):
                continue
            code = "jpn" if kind == "sub" else "eng"
            if code in languages:
                continue
            languages[code] = {
                "code": code,
                "name": f"{server} {'subtitled' if kind == 'sub' else 'dubbed'}",
                "embed_url": embed_url,
            }
            if len(languages) >= 2:
                break
        if not languages:
            raise AnimeSourceUnavailableError("stream servers were unavailable")
        return json.dumps({"languages": list(languages.values())}).encode("utf-8")

    async def fetch_embed(self, url: str) -> bytes:
        embed_url = _validate_embed_url(url)
        origin = urlsplit(embed_url)
        referer = f"{origin.scheme}://{origin.hostname}/"
        page = await self._get(embed_url, headers=("--referer", referer))
        match = re.search(r'window\.__P="([^"]*)"', _decode(page, "embed page"))
        if match is None:
            raise AnimeSourceUnavailableError("embed page had no player config")
        config = _deobfuscate_embed_blob(match.group(1))
        source = config.get("src")
        if not isinstance(source, str):
            raise AnimeSourceUnavailableError("embed player config had no stream URL")
        stream_url = _validate_stream_url(source)
        return f'<script>file:{json.dumps(stream_url)};</script>'.encode()

    async def fetch_playlist(self, url: str) -> bytes:
        return await self._get(url)

    @staticmethod
    def _normalize_query(query: str) -> str:
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 300 or _CONTROL_RE.search(normalized):
            raise AnimeSourceError("invalid anime search query")
        return normalized

    @staticmethod
    def _load_object(payload: bytes, label: str) -> dict[str, object]:
        document = _load_json(payload, label)
        if document.get("status") is not True:
            raise AnimeSourceUnavailableError(f"{label} was unavailable")
        return document


class AnimeTransport(Protocol):
    async def search(self, query: str) -> bytes: ...

    async def episodes(self, numeric_series_id: int) -> bytes: ...

    async def languages(self, episode_id: int) -> bytes: ...

    async def fetch_embed(self, url: str) -> bytes: ...

    async def fetch_playlist(self, url: str) -> bytes: ...


class AnimeSourceAdapter:
    def __init__(
        self,
        *,
        transport: AnimeTransport,
        preference_provider: Callable[[], Literal["sub", "dub"]],
        preference_writer: Callable[[Literal["sub", "dub"]], None] | None = None,
    ) -> None:
        self._transport = transport
        self._preference_provider = preference_provider
        self._preference_writer = preference_writer

    async def resolve(
        self,
        query: str,
        *,
        episode: int = 1,
        audio_mode: Literal["auto", "sub", "dub"] = "auto",
    ) -> AnimeResolution:
        normalized_query = " ".join(query.split())
        if (
            not normalized_query
            or len(normalized_query) > 300
            or _CONTROL_RE.search(normalized_query)
        ):
            raise AnimeSourceError("invalid anime search query")
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 1:
            raise MissingAnimeEpisodeError(episode)
        if audio_mode not in {"auto", "sub", "dub"}:
            raise AnimeSourceError("invalid anime audio mode")

        candidates = parse_search_html(await self._transport.search(normalized_query))
        selected = self._select_candidate(normalized_query, candidates)
        return await self._resolve_candidate(
            selected,
            normalized_query,
            episode=episode,
            audio_mode=audio_mode,
        )

    async def resolve_candidates(
        self,
        query: str,
        *,
        limit: int = _MAX_CHOICES,
    ) -> tuple[AnimeChoice, ...]:
        """Rank search candidates deterministically without resolving any stream."""
        normalized_query = " ".join(query.split())
        if (
            not normalized_query
            or len(normalized_query) > 300
            or _CONTROL_RE.search(normalized_query)
        ):
            raise AnimeSourceError("invalid anime search query")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CHOICES:
            raise AnimeSourceError(f"invalid candidate limit {limit}")
        candidates = parse_search_html(await self._transport.search(normalized_query))
        return _rank_candidates(normalized_query, candidates, limit)

    async def resolve_or_rank(
        self,
        query: str,
        *,
        episode: int = 1,
        audio_mode: Literal["auto", "sub", "dub"] = "auto",
    ) -> RankedResolution:
        """Resolve a clear winner directly; otherwise return ranked choices."""
        normalized_query = " ".join(query.split())
        if (
            not normalized_query
            or len(normalized_query) > 300
            or _CONTROL_RE.search(normalized_query)
        ):
            raise AnimeSourceError("invalid anime search query")
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 1:
            raise MissingAnimeEpisodeError(episode)
        if audio_mode not in {"auto", "sub", "dub"}:
            raise AnimeSourceError("invalid anime audio mode")

        candidates = parse_search_html(await self._transport.search(normalized_query))
        if len(candidates) == 1:
            resolved = await self._resolve_candidate(
                candidates[0],
                normalized_query,
                episode=episode,
                audio_mode=audio_mode,
            )
            return RankedResolution(kind="resolved", resolution=resolved, choices=())

        exact = tuple(
            item
            for item in candidates
            if _title_fit_score(normalized_query, item.title) >= _EXACT_SCORE
        )
        if len(exact) == 1:
            resolved = await self._resolve_candidate(
                exact[0],
                normalized_query,
                episode=episode,
                audio_mode=audio_mode,
            )
            return RankedResolution(kind="resolved", resolution=resolved, choices=())

        choices = _rank_candidates(normalized_query, candidates)
        top_score = _title_fit_score(normalized_query, choices[0].title)
        runner_up = (
            _title_fit_score(normalized_query, choices[1].title)
            if len(choices) > 1
            else 0.0
        )
        if top_score >= _EXACT_SCORE or (top_score - runner_up) >= _CLEAR_WIN_MARGIN:
            winner_id = choices[0].series_id
            winner = next(item for item in candidates if item.series_id == winner_id)
            resolved = await self._resolve_candidate(
                winner,
                normalized_query,
                episode=episode,
                audio_mode=audio_mode,
            )
            return RankedResolution(kind="resolved", resolution=resolved, choices=())
        return RankedResolution(kind="choices", resolution=None, choices=choices)

    async def _resolve_candidate(
        self,
        selected: AnimeCandidate,
        query: str,
        *,
        episode: int,
        audio_mode: Literal["auto", "sub", "dub"],
    ) -> AnimeResolution:
        del query  # the caller already validated and normalized it
        episodes = parse_episode_json(await self._transport.episodes(selected.numeric_id))
        available_numbers = tuple(item.number for item in episodes)
        selected_episode = next((item for item in episodes if item.number == episode), None)
        if selected_episode is None:
            # Series whose numbering does not include the requested episode (e.g. a
            # season continuing at 29): resolve the earliest available episode so the
            # controller's resume logic can still produce a valid proposal.
            if not episodes:
                raise MissingAnimeEpisodeError(episode)
            resolved_mode = self._preference_provider() if audio_mode == "auto" else audio_mode
            if resolved_mode not in {"sub", "dub"}:
                raise AnimeSourceError("stored anime audio preference is invalid")
            return await self._resolve_language_and_stream(
                selected,
                episodes[0],
                episode=episodes[0].number,
                resolved_mode=resolved_mode,
                available_numbers=available_numbers,
            )

        resolved_mode = self._preference_provider() if audio_mode == "auto" else audio_mode
        if resolved_mode not in {"sub", "dub"}:
            raise AnimeSourceError("stored anime audio preference is invalid")
        return await self._resolve_language_and_stream(
            selected,
            selected_episode,
            episode=episode,
            resolved_mode=resolved_mode,
            available_numbers=available_numbers,
        )

    async def _resolve_language_and_stream(
        self,
        selected: AnimeCandidate,
        selected_episode: AnimeEpisode,
        *,
        episode: int,
        resolved_mode: Literal["sub", "dub"],
        available_numbers: tuple[int, ...],
    ) -> AnimeResolution:
        language_code = "jpn" if resolved_mode == "sub" else "eng"
        language_payload = await self._transport.languages(selected_episode.episode_id)
        languages = parse_languages_json(language_payload)
        if not languages:
            raise MissingAnimeLanguageError(resolved_mode, ())
        language = next((item for item in languages if item.code == language_code), None)
        if language is None:
            available: list[Literal["sub", "dub"]] = []
            if any(item.code == "jpn" for item in languages):
                available.append("sub")
            if any(item.code == "eng" for item in languages):
                available.append("dub")
            raise MissingAnimeLanguageError(resolved_mode, tuple(available))

        master_url = parse_embed_html(await self._transport.fetch_embed(language.embed_url))
        variants = parse_master_playlist(
            await self._transport.fetch_playlist(master_url),
            master_url,
        )
        stream_url = select_best_variant(variants).url
        label = "Subbed" if resolved_mode == "sub" else "Dubbed"
        return AnimeResolution(
            series_id=selected.series_id,
            series_title=selected.title,
            episode=episode,
            audio_mode=resolved_mode,
            subtitle=f"Episode {episode} · {label}",
            poster_url=selected.poster_url,
            stream_url=stream_url,
            available_episode_numbers=available_numbers,
        )

    @staticmethod
    def _select_candidate(
        query: str,
        candidates: tuple[AnimeCandidate, ...],
    ) -> AnimeCandidate:
        folded = query.casefold()
        exact = tuple(item for item in candidates if item.title.casefold() == folded)
        if len(exact) == 1:
            return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        raise AnimeAmbiguityError(tuple(item.title for item in candidates[:5]))
