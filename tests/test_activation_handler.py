from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ACTIVATION = {
    "type": "activation",
    "probability": 0.88,
    "detected_at": 42.25,
    "native_listener": True,
}


async def _run_handler(payload: bytes, path: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["ACTIVATION_SOCKET_PATH"] = str(path)
    project_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH")
    python_paths = [str(project_root / "src")]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "realtime_action_spike.activation_handler",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=2.0)
    return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def _serve_once(
    path: Path,
    response: dict[str, object] | None,
    *,
    release: asyncio.Event | None = None,
    received: asyncio.Event | None = None,
) -> asyncio.AbstractServer:
    path.parent.mkdir(parents=True, exist_ok=True)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        parsed = json.loads(line)
        assert parsed == ACTIVATION
        if received is not None:
            received.set()
        if release is not None:
            await release.wait()
        if response is not None:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handle, path=str(path))


@pytest.mark.asyncio
async def test_completed_handler_waits_for_terminal_socket_response(tmp_path: Path) -> None:
    path = tmp_path / "activation.sock"
    release = asyncio.Event()
    received = asyncio.Event()
    server = await _serve_once(
        path,
        {"outcome": "completed", "session_id": "local-session-01"},
        release=release,
        received=received,
    )

    task = asyncio.create_task(_run_handler(json.dumps(ACTIVATION).encode(), path))
    await asyncio.wait_for(received.wait(), timeout=1.0)
    await asyncio.sleep(0.02)
    assert not task.done()

    release.set()
    code, stdout, stderr = await task
    assert code == 0
    assert json.loads(stdout) == {
        "outcome": "completed",
        "session_id": "local-session-01",
    }
    assert stderr == ""

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_busy_response_uses_distinct_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "activation.sock"
    server = await _serve_once(path, {"outcome": "busy"})

    code, stdout, stderr = await _run_handler(json.dumps(ACTIVATION).encode(), path)
    assert code == 2
    assert json.loads(stdout) == {"outcome": "busy"}
    assert stderr == ""

    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"", b"not-json", b"{}", b"{} {}"])
async def test_malformed_stdin_is_sanitized(payload: bytes, tmp_path: Path) -> None:
    code, stdout, stderr = await _run_handler(payload, tmp_path / "missing.sock")

    assert code == 1
    assert json.loads(stdout) == {
        "outcome": "failed",
        "error": "invalid activation payload",
    }
    assert "Traceback" not in stderr


@pytest.mark.asyncio
async def test_missing_socket_is_sanitized(tmp_path: Path) -> None:
    code, stdout, stderr = await _run_handler(
        json.dumps(ACTIVATION).encode(),
        tmp_path / "missing" / "activation.sock",
    )

    assert code == 1
    assert json.loads(stdout) == {
        "outcome": "failed",
        "error": "socket unavailable",
    }
    assert "Traceback" not in stderr


@pytest.mark.asyncio
async def test_controller_disconnect_is_failed_without_traceback(tmp_path: Path) -> None:
    path = tmp_path / "activation.sock"
    server = await _serve_once(path, None)

    code, stdout, stderr = await _run_handler(json.dumps(ACTIVATION).encode(), path)
    assert code == 1
    assert json.loads(stdout) == {
        "outcome": "failed",
        "error": "activation failed",
    }
    assert "Traceback" not in stderr

    server.close()
    await server.wait_closed()


def test_default_socket_path_matches_runtime_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from realtime_action_spike.activation_handler import _resolve_socket_path

    monkeypatch.delenv("ACTIVATION_SOCKET_PATH", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    assert _resolve_socket_path() == "/run/user/1000/okay-hermes-realtime/activation.sock"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 3600.0),
        ("900", 900.0),
    ],
)
def test_result_wait_timeout_is_realistic_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: float,
) -> None:
    from realtime_action_spike.activation_handler import _result_timeout_seconds

    if configured is None:
        monkeypatch.delenv("ACTIVATION_RESULT_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ACTIVATION_RESULT_TIMEOUT_SECONDS", configured)

    assert _result_timeout_seconds() == expected


@pytest.mark.parametrize("configured", ["0", "86401", "not-a-number"])
def test_result_wait_timeout_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    from realtime_action_spike.activation_handler import _result_timeout_seconds

    monkeypatch.setenv("ACTIVATION_RESULT_TIMEOUT_SECONDS", configured)

    with pytest.raises(RuntimeError, match="invalid activation result timeout"):
        _result_timeout_seconds()
