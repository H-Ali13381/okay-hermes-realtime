from __future__ import annotations

import asyncio
import contextlib
import json
import socket as socket_module
import stat
from pathlib import Path

from .controller import VoiceSessionController
from .protocol import ActivationMessage, SessionOutcome, parse_loopback_message

MAX_ACTIVATION_REQUEST_BYTES = 2048
MAX_ACTIVATION_RESPONSE_BYTES = 2048
SOCKET_PROBE_TIMEOUT_SECONDS = 0.1
REQUEST_TIMEOUT_SECONDS = 1.0


class ActivationSocketError(RuntimeError):
    """Raised when activation socket transport or protocol handling fails."""


class ActivationSocket:
    """Unix-socket bridge for native activation handoff."""

    def __init__(
        self,
        controller: VoiceSessionController,
        voice_page_url: str,
        path: str,
        *,
        max_request_bytes: int = MAX_ACTIVATION_REQUEST_BYTES,
    ) -> None:
        self._controller = controller
        self._voice_page_url = voice_page_url
        self._path = Path(path)
        self._max_request_bytes = max_request_bytes

        self._server: asyncio.AbstractServer | None = None
        self._path_identity: tuple[int, int] | None = None
        self.started = asyncio.Event()

    async def start(self) -> None:
        self._prepare_socket_path()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._path),
            backlog=20,
        )
        socket_stat = self._path.stat()
        self._path_identity = (socket_stat.st_dev, socket_stat.st_ino)

        self._path.chmod(0o600)
        self._path.parent.chmod(0o700)
        self.started.set()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._path.exists():
            self._cleanup_socket_file()
        self.started.clear()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(
                self._read_request_line(reader),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            trailing = await asyncio.wait_for(
                reader.read(1),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if trailing:
                raise ActivationSocketError("trailing request data")
            _parse_activation_message(request_line)

            activation = await self._controller.activate(self._voice_page_url)
            if activation.status == "busy":
                await _write_response({"outcome": "busy"}, writer)
                return

            if activation.status == "failed":
                await _write_response(
                    {
                        "outcome": "failed",
                        "error": _sanitize_error(activation.error),
                    },
                    writer,
                )
                return

            terminal_result = await asyncio.shield(
                self._controller.wait_for_terminal_result(activation.session_id or "")
            )
            if terminal_result.outcome == SessionOutcome.COMPLETED:
                await _write_response(
                    {
                        "outcome": "completed",
                        "session_id": terminal_result.session_id,
                    },
                    writer,
                )
                return

            if terminal_result.outcome == SessionOutcome.CANCELLED:
                await _write_response(
                    {
                        "outcome": "cancelled",
                        "session_id": terminal_result.session_id,
                    },
                    writer,
                )
                return

            await _write_response(
                {
                    "outcome": "failed",
                    "error": _sanitize_error(terminal_result.error),
                },
                writer,
            )
        except TimeoutError:
            await _write_response({"outcome": "failed", "error": "protocol timeout"}, writer)
        except ActivationSocketError:
            await _write_response({"outcome": "failed", "error": "protocol error"}, writer)
        except Exception:
            await _write_response({"outcome": "failed", "error": "activation failed"}, writer)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _read_request_line(self, reader: asyncio.StreamReader) -> bytes:
        line = await reader.readline()
        if not line:
            raise ActivationSocketError("empty request")
        if len(line) > self._max_request_bytes:
            raise ActivationSocketError("request too large")
        if not line.endswith(b"\n"):
            raise ActivationSocketError("missing newline")

        if b"\n" in line[:-1]:
            raise ActivationSocketError("multiple lines")

        payload = line[:-1].rstrip(b"\r")
        if len(payload) > self._max_request_bytes:
            raise ActivationSocketError("request too large")
        if not payload:
            raise ActivationSocketError("empty request")
        return payload


    def _prepare_socket_path(self) -> None:
        if not self._path.is_absolute():
            raise ActivationSocketError("activation socket path must be absolute")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)

        try:
            existing_stat = self._path.lstat()
        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(existing_stat.st_mode):
            raise ActivationSocketError("activation socket path is not a socket")

        if self._probe_socket():
            raise ActivationSocketError("activation socket already running")

        refreshed_stat = self._path.lstat()
        if (
            refreshed_stat.st_mode != existing_stat.st_mode
            or refreshed_stat.st_dev != existing_stat.st_dev
            or refreshed_stat.st_ino != existing_stat.st_ino
        ):
            raise ActivationSocketError("activation socket changed while checking")

        self._path.unlink()

    def _probe_socket(self) -> bool:
        probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        probe.settimeout(SOCKET_PROBE_TIMEOUT_SECONDS)
        try:
            probe.connect(str(self._path))
            return True
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        except OSError:
            return False
        finally:
            with contextlib.suppress(Exception):
                probe.close()

    def _cleanup_socket_file(self) -> None:
        try:
            current_stat = self._path.lstat()
        except FileNotFoundError:
            return

        if not stat.S_ISSOCK(current_stat.st_mode):
            return

        if self._path_identity is None:
            return

        if (current_stat.st_dev, current_stat.st_ino) != self._path_identity:
            return

        with contextlib.suppress(Exception):
            self._path.unlink()


def _parse_activation_message(payload: bytes) -> ActivationMessage:
    try:
        message = parse_loopback_message(payload)
    except (TypeError, ValueError) as exc:
        raise ActivationSocketError("invalid activation message") from exc
    if not isinstance(message, ActivationMessage):
        raise ActivationSocketError("not activation message")
    if not message.native_listener:
        raise ActivationSocketError("native listener not enabled")
    return message


def _sanitize_error(raw_error: str | None) -> str:
    return "activation failed"


def _json_dumps(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


async def _write_response(payload: dict[str, object], writer: asyncio.StreamWriter) -> None:
    data = _json_dumps(payload)
    if len(data) > MAX_ACTIVATION_RESPONSE_BYTES:
        data = b'{"outcome":"failed","error":"response too large"}'
    writer.write(data + b"\n")
    await writer.drain()
