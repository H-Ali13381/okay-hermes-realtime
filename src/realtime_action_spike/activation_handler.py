from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from .runtime.protocol import ActivationMessage, parse_loopback_message

MAX_ACTIVATION_PAYLOAD_BYTES = 2048
MAX_ACTIVATION_RESPONSE_BYTES = 2048
DEFAULT_RESULT_TIMEOUT_SECONDS = 3600.0
MAX_RESULT_TIMEOUT_SECONDS = 86_400.0


def main() -> int:
    try:
        activation = _parse_stdin_message()
    except ValueError:
        print(json.dumps({"outcome": "failed", "error": "invalid activation payload"}))
        return 1

    if not activation.native_listener:
        print(json.dumps({"outcome": "failed", "error": "invalid activation payload"}))
        return 1

    try:
        response = asyncio.run(_request_activation(activation))
    except FileNotFoundError:
        print(json.dumps({"outcome": "failed", "error": "socket unavailable"}))
        return 1
    except OSError:
        print(json.dumps({"outcome": "failed", "error": "socket unavailable"}))
        return 1
    except Exception:
        print(json.dumps({"outcome": "failed", "error": "activation failed"}))
        return 1

    outcome = response.get("outcome")
    print(json.dumps(response))

    if outcome == "completed":
        return 0
    if outcome == "busy":
        return 2
    return 1


def _parse_stdin_message() -> ActivationMessage:
    import sys

    raw = sys.stdin.buffer.read(MAX_ACTIVATION_PAYLOAD_BYTES + 1)
    if not raw:
        raise ValueError("empty payload")
    if len(raw) > MAX_ACTIVATION_PAYLOAD_BYTES:
        raise ValueError("payload too large")

    text = _decode_single_json(raw)
    parsed = parse_loopback_message(text)
    if not isinstance(parsed, ActivationMessage):
        raise ValueError("not activation message")
    return parsed


def _decode_single_json(raw: bytes) -> str:
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    payload, consumed = decoder.raw_decode(text)
    if not isinstance(payload, dict):
        raise ValueError("payload must be object")
    trailing = text[consumed:].strip()
    if trailing:
        raise ValueError("trailing payload")
    return text


async def _request_activation(message: ActivationMessage) -> dict[str, object]:
    path = _resolve_socket_path()
    payload = json.dumps(message.model_dump(mode="json"), separators=(",", ":")) + "\n"

    reader, writer = await asyncio.open_unix_connection(path)
    try:
        if len(payload.encode("utf-8")) > MAX_ACTIVATION_PAYLOAD_BYTES:
            raise ValueError("payload too large")

        writer.write(payload.encode("utf-8"))
        with contextlib.suppress(Exception):
            writer.write_eof()
        await writer.drain()

        response_line = await asyncio.wait_for(
            reader.readline(),
            timeout=_result_timeout_seconds(),
        )
        if not response_line:
            raise RuntimeError("empty response")
        if len(response_line) > MAX_ACTIVATION_RESPONSE_BYTES:
            raise RuntimeError("response too large")

        try:
            tail = await asyncio.wait_for(reader.read(1), timeout=0.05)
            if tail.strip():
                raise ValueError("trailing data")
        except TimeoutError:
            pass

        return _decode_response(response_line.decode("utf-8"))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _decode_response(raw: str) -> dict[str, object]:
    response = json.loads(raw.strip())
    if not isinstance(response, dict) or "outcome" not in response:
        raise RuntimeError("invalid response")
    if response["outcome"] == "completed":
        return {"outcome": "completed", "session_id": str(response.get("session_id", ""))}
    if response["outcome"] == "busy":
        return {"outcome": "busy"}

    return {
        "outcome": "failed",
        "error": _sanitize_error(response.get("error")),
    }


def _sanitize_error(error: object) -> str:
    if not isinstance(error, str) or not error:
        return "activation failed"
    return "activation failed"


def _resolve_socket_path() -> str:
    env_override = os.getenv("ACTIVATION_SOCKET_PATH")
    if env_override:
        return str(Path(env_override))

    runtime_dir = os.getenv("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is required for activation socket path")

    return str(Path(runtime_dir) / "okay-hermes-realtime" / "activation.sock")


def _result_timeout_seconds() -> float:
    raw = os.getenv("ACTIVATION_RESULT_TIMEOUT_SECONDS", str(DEFAULT_RESULT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError("invalid activation result timeout") from exc
    if not 1.0 <= value <= MAX_RESULT_TIMEOUT_SECONDS:
        raise RuntimeError("invalid activation result timeout")
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
