from __future__ import annotations

import asyncio
import json
import socket
import stat
from pathlib import Path

import pytest

from realtime_action_spike.runtime.activation_socket import (
    ActivationSocket,
    ActivationSocketError,
)
from realtime_action_spike.runtime.controller import (
    ActivationResult,
    TerminalSessionResult,
)
from realtime_action_spike.runtime.protocol import SessionOutcome

ACTIVATION = {
    "type": "activation",
    "probability": 0.91,
    "detected_at": 12.5,
    "native_listener": True,
}


class FakeController:
    def __init__(self, activation: ActivationResult) -> None:
        self.activation = activation
        self.terminal_future: asyncio.Future[TerminalSessionResult] | None = None
        self.wait_started = asyncio.Event()

    async def activate(self, _voice_page_url: str) -> ActivationResult:
        return self.activation

    async def wait_for_terminal_result(self, session_id: str) -> TerminalSessionResult:
        self.wait_started.set()
        if self.terminal_future is None:
            self.terminal_future = asyncio.get_running_loop().create_future()
        result = await asyncio.shield(self.terminal_future)
        assert result.session_id == session_id
        return result


async def _open_request(
    path: Path,
    payload: bytes,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(payload)
    await writer.drain()
    writer.write_eof()
    return reader, writer


async def _read_json_line(reader: asyncio.StreamReader) -> dict[str, object]:
    line = await asyncio.wait_for(reader.readline(), timeout=1.0)
    assert line.endswith(b"\n")
    payload = json.loads(line)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.asyncio
async def test_valid_handoff_blocks_until_exact_session_closes(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    controller = FakeController(
        ActivationResult(status="opened", session_id="local-session-01", token="launch-token")
    )
    server = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]
    await server.start()

    reader, writer = await _open_request(path, json.dumps(ACTIVATION).encode() + b"\n")
    await asyncio.wait_for(controller.wait_started.wait(), timeout=1.0)
    await asyncio.sleep(0.02)
    assert not reader.at_eof()

    assert controller.terminal_future is not None
    controller.terminal_future.set_result(
        TerminalSessionResult(
            session_id="local-session-01",
            outcome=SessionOutcome.COMPLETED,
        )
    )
    assert await _read_json_line(reader) == {
        "outcome": "completed",
        "session_id": "local-session-01",
    }

    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_busy_and_malformed_requests_return_bounded_responses(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    controller = FakeController(ActivationResult(status="busy"))
    server = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]
    await server.start()

    reader, writer = await _open_request(path, json.dumps(ACTIVATION).encode() + b"\n")
    assert await _read_json_line(reader) == {"outcome": "busy"}
    writer.close()
    await writer.wait_closed()

    reader, writer = await _open_request(path, b"not-json\n")
    assert await _read_json_line(reader) == {
        "error": "protocol error",
        "outcome": "failed",
    }
    writer.close()
    await writer.wait_closed()

    reader, writer = await _open_request(
        path,
        json.dumps(ACTIVATION).encode() + b"\n{}\n",
    )
    assert await _read_json_line(reader) == {
        "error": "protocol error",
        "outcome": "failed",
    }
    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_socket_and_parent_are_owner_only_and_removed_on_stop(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    controller = FakeController(ActivationResult(status="busy"))
    server = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]

    await server.start()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    await server.stop()
    assert not path.exists()


@pytest.mark.asyncio
async def test_stale_socket_is_recovered_but_healthy_socket_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    path.parent.mkdir()

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()

    controller = FakeController(ActivationResult(status="busy"))
    server = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]
    await server.start()
    await server.stop()

    healthy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    healthy.bind(str(path))
    healthy.listen(1)
    try:
        other = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]
        with pytest.raises(ActivationSocketError, match="already running"):
            await other.start()
    finally:
        healthy.close()
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_non_socket_path_is_never_removed(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    path.parent.mkdir()
    path.write_text("do not delete", encoding="utf-8")
    server = ActivationSocket(
        FakeController(ActivationResult(status="busy")),  # type: ignore[arg-type]
        "http://127.0.0.1:8765/voice",
        str(path),
    )

    with pytest.raises(ActivationSocketError, match="not a socket"):
        await server.start()
    assert path.read_text(encoding="utf-8") == "do not delete"


@pytest.mark.asyncio
async def test_client_disconnect_does_not_cancel_terminal_future(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "activation.sock"
    controller = FakeController(
        ActivationResult(status="opened", session_id="local-session-02", token="launch-token")
    )
    server = ActivationSocket(controller, "http://127.0.0.1:8765/voice", str(path))  # type: ignore[arg-type]
    await server.start()

    _reader, writer = await _open_request(path, json.dumps(ACTIVATION).encode() + b"\n")
    await asyncio.wait_for(controller.wait_started.wait(), timeout=1.0)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.02)

    assert controller.terminal_future is not None
    assert not controller.terminal_future.cancelled()
    controller.terminal_future.set_result(
        TerminalSessionResult(
            session_id="local-session-02",
            outcome=SessionOutcome.COMPLETED,
        )
    )
    await asyncio.sleep(0.02)
    assert controller.terminal_future.result().outcome is SessionOutcome.COMPLETED
    await server.stop()
