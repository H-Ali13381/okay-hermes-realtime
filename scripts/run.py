"""Launch the loopback Realtime gateway and Streamlit UI as one process group."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from realtime_action_spike.config import Settings  # noqa: E402


def build_commands(
    settings: Settings,
    root: Path = ROOT,
) -> tuple[list[str], list[str], dict[str, str]]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    python_paths = [str(root / "src")]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)

    gateway = [
        sys.executable,
        "-m",
        "uvicorn",
        "realtime_action_spike.gateway:app",
        "--host",
        settings.gateway_host,
        "--port",
        str(settings.gateway_port),
    ]
    streamlit = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(root / "src/realtime_action_spike/streamlit_app.py"),
        "--server.address",
        settings.streamlit_host,
        "--server.port",
        str(settings.streamlit_port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    return gateway, streamlit, environment


def wait_for_health(
    url: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: float = 20,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"gateway exited before readiness with status {return_code}")
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"gateway did not become healthy at {url}: {last_error}")


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main(_arguments: Sequence[str] | None = None) -> int:
    os.chdir(ROOT)
    settings = Settings.from_env()
    gateway_command, streamlit_command, environment = build_commands(settings)
    gateway_process: subprocess.Popen[bytes] | None = None
    streamlit_process: subprocess.Popen[bytes] | None = None
    stopping = False

    def request_stop(_signal_number: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        gateway_process = subprocess.Popen(gateway_command, cwd=ROOT, env=environment)
        health_url = f"http://{settings.gateway_host}:{settings.gateway_port}/health"
        wait_for_health(health_url, gateway_process)

        streamlit_process = subprocess.Popen(streamlit_command, cwd=ROOT, env=environment)
        page_url = f"http://{settings.streamlit_host}:{settings.streamlit_port}"
        print(f"Realtime action test: {page_url}", flush=True)
        if settings.api_key_value() is None:
            print(
                "OPENAI_API_KEY is not configured; the UI will start but sessions cannot.",
                flush=True,
            )

        while not stopping:
            gateway_status = gateway_process.poll()
            streamlit_status = streamlit_process.poll()
            if gateway_status is not None:
                return gateway_status or 1
            if streamlit_status is not None:
                return streamlit_status
            time.sleep(0.25)
        return 0
    finally:
        stop_process(streamlit_process)
        stop_process(gateway_process)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
