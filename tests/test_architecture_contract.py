from __future__ import annotations

import json
from pathlib import Path

from pydantic import SecretStr

from realtime_action_spike.config import Settings
from realtime_action_spike.gateway import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "realtime_action_spike"


def _settings() -> Settings:
    return Settings(openai_api_key=SecretStr("sk-test"))


def test_runtime_has_one_openai_transport_path() -> None:
    routes = {
        path
        for route in create_app(_settings()).routes
        if (path := getattr(route, "path", None))
    }
    assert "/session" in routes
    assert "/execute" in routes
    assert "/client-secret" not in routes

    for path in [
        SOURCE_ROOT / "openai" / "calls.py",
        SOURCE_ROOT / "openai" / "events.py",
        SOURCE_ROOT / "openai" / "sideband.py",
        SOURCE_ROOT / "openai" / "tool_loop.py",
    ]:
        assert not path.exists()

    combined_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SOURCE_ROOT.rglob("*.py")
    )
    for retired_symbol in [
        "RealtimeConnectedMessage",
        "ActionStateMessage",
        "RealtimeSidebandClient",
        "start_realtime_sideband",
        "provider_call_id",
    ]:
        assert retired_symbol not in combined_source

    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    dependencies = package.get("dependencies", {})
    assert "@openai/agents-realtime" not in dependencies
    assert "zod" not in dependencies
