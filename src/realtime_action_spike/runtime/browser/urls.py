from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .contracts import BrowserLaunchError


def validate_loopback_base_url(url: str) -> str:
    parsed = _parse_loopback_url(url)
    if parsed.query:
        raise BrowserLaunchError("loopback base URL must not include query parameters")
    if parsed.fragment:
        raise BrowserLaunchError("loopback base URL must not include a fragment")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserLaunchError("loopback base URL must not include credentials")
    if not _is_loopback_host(parsed.hostname):
        raise BrowserLaunchError("loopback base URL must be loopback")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def build_activation_loopback_url(base_url: str, activation_token: str) -> str:
    parsed = urlsplit(base_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["activation"] = [activation_token]
    encoded = urlencode(query, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded, ""))


def validate_activation_loopback_url(candidate_url: str, expected_base: str) -> str:
    candidate = _parse_loopback_url(candidate_url)
    expected = _parse_loopback_url(expected_base)

    if (
        candidate.scheme,
        candidate.netloc,
        candidate.path,
    ) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise BrowserLaunchError("loopback URL is not based on configured voice page URL")

    if candidate.fragment:
        raise BrowserLaunchError("loopback URL must not include a fragment")
    if candidate.username is not None or candidate.password is not None:
        raise BrowserLaunchError("loopback URL must not include credentials")
    if not _is_loopback_host(candidate.hostname):
        raise BrowserLaunchError("loopback URL must target a local loopback host")

    query = parse_qs(candidate.query, keep_blank_values=True)
    if set(query.keys()) != {"activation"}:
        raise BrowserLaunchError("loopback URL may only include the activation query parameter")
    if len(query["activation"]) != 1 or not query["activation"][0]:
        raise BrowserLaunchError("loopback URL must include activation token")

    return urlunsplit(
        (
            candidate.scheme,
            candidate.netloc,
            candidate.path,
            urlencode({"activation": query["activation"]}, doseq=True),
            "",
        )
    )


def _parse_loopback_url(url: str):
    parsed = urlsplit(url)
    if not parsed.scheme:
        raise BrowserLaunchError("loopback URL must include a scheme")
    if parsed.scheme not in {"http", "https"}:
        raise BrowserLaunchError("loopback URL must use http or https")
    if not parsed.hostname:
        raise BrowserLaunchError("loopback URL must include a hostname")
    return parsed


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}
