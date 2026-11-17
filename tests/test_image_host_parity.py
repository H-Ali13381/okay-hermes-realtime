from __future__ import annotations

import json
from importlib.resources import files

from realtime_action_spike.anime import _POSTER_PATH_PREFIXES
from realtime_action_spike.runtime.protocol import _ALLOWED_THUMBNAIL_HOSTS


def test_catalog_protocol_and_shared_image_hosts_are_in_sync() -> None:
    shared_hosts = set(
        json.loads(
            files("realtime_action_spike")
            .joinpath("media_image_hosts.json")
            .read_text(encoding="utf-8")
        )["hosts"]
    )

    assert set(_POSTER_PATH_PREFIXES) <= shared_hosts
    assert shared_hosts == _ALLOWED_THUMBNAIL_HOSTS
