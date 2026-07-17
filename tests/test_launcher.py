from __future__ import annotations

import sys
from pathlib import Path

from realtime_action_spike.config import Settings
from scripts.run import build_commands


def test_launcher_commands_bind_both_services_to_configured_loopback_ports() -> None:
    root = Path("/tmp/realtime-spike")
    settings = Settings(
        openai_api_key="must-not-appear",
        gateway_host="127.0.0.1",
        gateway_port=9876,
        streamlit_host="127.0.0.1",
        streamlit_port=9877,
    )

    gateway, streamlit, environment = build_commands(settings, root)

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
    assert streamlit == [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(root / "src/realtime_action_spike/streamlit_app.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        "9877",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    assert environment["PYTHONPATH"].split(":")[0] == str(root / "src")
    assert "must-not-appear" not in " ".join(gateway + streamlit)
