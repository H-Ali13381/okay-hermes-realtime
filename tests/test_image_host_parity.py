from __future__ import annotations

import re
from pathlib import Path

from realtime_action_spike.anime import _POSTER_PATH_PREFIXES
from realtime_action_spike.runtime.protocol import _ALLOWED_THUMBNAIL_HOSTS


def test_catalog_protocol_and_browser_image_hosts_are_in_sync() -> None:
    frontend = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "media-confirmation.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"const IMAGE_HOSTS = new Set\(\[(.*?)\]\)", frontend, re.S)
    assert match, "frontend image host allowlist not found"
    browser_hosts = set(re.findall(r'"([a-z0-9.-]+)"', match.group(1)))

    assert set(_POSTER_PATH_PREFIXES) <= _ALLOWED_THUMBNAIL_HOSTS
    assert set(_POSTER_PATH_PREFIXES) <= browser_hosts
