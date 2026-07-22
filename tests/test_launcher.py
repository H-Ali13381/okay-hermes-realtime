from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import SecretStr

from realtime_action_spike.config import Settings
from scripts.run import build_commands


def test_launcher_starts_a_single_gateway_process_on_configured_host_and_port() -> None:
    root = Path("/tmp/realtime-spike")
    settings = Settings(
        openai_api_key=SecretStr("must-not-appear"),
        gateway_host="127.0.0.1",
        gateway_port=9876,
    )

    gateway, environment = build_commands(settings, root)

    assert gateway == [
        sys.executable,
        "-m",
        "uvicorn",
        "realtime_action_spike.gateway:app",
        "--host",
        "127.0.0.1",
        "--port",
        "9876",
    ]
    assert "streamlit" not in gateway
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(root / "src")
    assert "must-not-appear" not in " ".join(gateway)
