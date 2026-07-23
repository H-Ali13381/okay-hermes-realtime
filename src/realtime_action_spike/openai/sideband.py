from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosedError

JsonObject = Mapping[str, Any]
MAX_SIDEBAND_EVENT_BYTES = 256_000
# Hard ceiling on a single raw frame before parsing. OpenAI can embed the full
# input audio (~1MB base64) in retrieved conversation items; we allow the frame
# in, strip the audio we do not use, then apply MAX_SIDEBAND_EVENT_BYTES.
MAX_SIDEBAND_FRAME_BYTES = 4_000_000

logger = logging.getLogger(__name__)

DEFAULT_RECONNECT_DELAYS = (0.15, 0.5)


@dataclass(frozen=True)
class SidebandEvent:
    """Structured event envelope from the OpenAI sideband WebSocket."""

    local_session_id: str
    payload: JsonObject


def build_sideband_url(call_id: str) -> str:
    """Build the official sideband URL for a specific Realtime call."""

    if not call_id:
        raise ValueError("call_id must be non-empty")
    encoded = quote(call_id, safe="")
    return f"wss://api.openai.com/v1/realtime?call_id={encoded}"


# Keys whose (potentially huge) string values the server sideband never uses.
# Base64 input/output audio embedded in retrieved conversation items falls here.
_UNUSED_LARGE_STRING_KEYS = ("audio",)


def _strip_embedded_audio(node: Any) -> None:
    """Recursively drop unused large audio strings from a parsed event in place."""

    if isinstance(node, dict):
        for key in _UNUSED_LARGE_STRING_KEYS:
            value = node.get(key)
            if isinstance(value, str):
                node[key] = ""
        for value in node.values():
            _strip_embedded_audio(value)
    elif isinstance(node, list):
        for item in node:
            _strip_embedded_audio(item)


class RealtimeSidebandClient:
    """Minimal OpenAI sideband client with ordered event routing and one writer lock."""

    def __init__(
        self,
        *,
        local_session_id: str,
        call_id: str,
        api_key: str,
        on_event: Callable[[SidebandEvent], Awaitable[None]],
        on_terminal_failure: Callable[[str, Exception], Awaitable[None]],
        websocket_connect: Callable[[str, dict[str, str]], Awaitable[Any]] | None = None,
        reconnect_delays: tuple[float, ...] = DEFAULT_RECONNECT_DELAYS,
    ) -> None:
        if not local_session_id:
            raise ValueError("local_session_id must be non-empty")
        if not call_id:
            raise ValueError("call_id must be non-empty")
        if not api_key:
            raise ValueError("api_key must be non-empty")

        self.local_session_id = local_session_id
        self.call_id = call_id
        self._api_key = api_key
        self._on_event = on_event
        self._on_terminal_failure = on_terminal_failure
        self._connect = websocket_connect or self._default_websocket_connect
        self._reconnect_delays = reconnect_delays

        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._closed = False
        self._failed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("sideband client already closed")
            if self._websocket is not None:
                return

            try:
                websocket = await self._open_websocket()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._fail(error)
                raise

            if self._closed:
                with contextlib.suppress(Exception):
                    await websocket.close()
                raise RuntimeError("sideband client closed during connect")

            self._websocket = websocket
            self._reader_task = asyncio.create_task(self._run_reader())

    async def send_json(self, payload: JsonObject) -> None:
        if self._closed:
            raise RuntimeError("sideband client is closed")
        data = json.dumps(payload, separators=(",", ":"))

        async with self._send_lock:
            websocket = self._websocket
            if websocket is None:
                raise RuntimeError("sideband is not connected")
            await websocket.send(data)

    async def close(self) -> None:
        self._closed = True
        websocket = self._websocket
        self._websocket = None
        reader_task = self._reader_task
        self._reader_task = None

        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()
        current_task = asyncio.current_task()
        if (
            reader_task is not None
            and reader_task is not current_task
            and not reader_task.done()
        ):
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task

    async def _run_reader(self) -> None:
        while not self._closed:
            websocket = self._websocket
            if websocket is None:
                return
            try:
                while True:
                    raw_message = await websocket.recv()
                    message = self._decode_message(raw_message)
                    await self._on_event(
                        SidebandEvent(local_session_id=self.local_session_id, payload=message)
                    )
            except asyncio.CancelledError:
                return
            except Exception as error:
                reattached, terminal_error = await self._reattach_after_abnormal_close(
                    error,
                    websocket,
                )
                if reattached:
                    continue
                logger.error(
                    "sideband_reader_failure session_id=%s error_type=%s error=%s",
                    self.local_session_id,
                    type(terminal_error).__name__,
                    terminal_error,
                    exc_info=(
                        type(terminal_error),
                        terminal_error,
                        terminal_error.__traceback__,
                    ),
                )
                await self._fail(terminal_error)
                return

    async def _reattach_after_abnormal_close(
        self,
        error: Exception,
        failed_websocket: Any,
    ) -> tuple[bool, Exception]:
        if (
            not isinstance(error, ConnectionClosedError)
            or error.rcvd is not None
            or error.sent is not None
        ):
            return False, error

        if self._websocket is failed_websocket:
            self._websocket = None
        with contextlib.suppress(Exception):
            await failed_websocket.close()

        last_error: Exception = error
        for attempt, delay in enumerate(self._reconnect_delays, start=1):
            if self._closed:
                return False, last_error
            logger.warning(
                "sideband_reattach_attempt session_id=%s attempt=%d status_code=1006",
                self.local_session_id,
                attempt,
            )
            await asyncio.sleep(delay)
            if self._closed:
                return False, last_error
            try:
                websocket = await self._open_websocket()
            except asyncio.CancelledError:
                raise
            except Exception as reconnect_error:
                last_error = reconnect_error
                continue
            if self._closed:
                with contextlib.suppress(Exception):
                    await websocket.close()
                return False, last_error
            self._websocket = websocket
            logger.warning(
                "sideband_reattached session_id=%s attempt=%d",
                self.local_session_id,
                attempt,
            )
            return True, last_error
        return False, last_error

    async def _open_websocket(self) -> Any:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        return await self._connect(build_sideband_url(self.call_id), headers)

    def _decode_message(self, raw_message: Any) -> JsonObject:
        raw_text = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        if not isinstance(raw_text, str):
            raise TypeError("sideband message must be text")
        # Hard ceiling guards against a pathological frame before we even parse.
        # OpenAI can legitimately echo conversation items that embed the full
        # input audio as base64 (~1MB), so this ceiling is generous; the real
        # semantic cap is applied after we drop audio we never consume.
        if len(raw_text.encode("utf-8")) > MAX_SIDEBAND_FRAME_BYTES:
            raise ValueError("sideband frame exceeds hard ceiling")

        message = json.loads(raw_text)
        if not isinstance(message, dict):
            raise TypeError("sideband message payload must be a JSON object")

        # Suppress embedded audio: the server sideband only consumes function-call
        # and response lifecycle events. Base64 audio in retrieved conversation
        # items is never used here and previously blew past the size cap, killing
        # otherwise-healthy sessions. Strip it before the semantic size check.
        _strip_embedded_audio(message)

        encoded_size = len(json.dumps(message, separators=(",", ":")).encode("utf-8"))
        if encoded_size > MAX_SIDEBAND_EVENT_BYTES:
            raise ValueError("sideband message is too large")
        return message

    async def _fail(self, error: Exception) -> None:
        if self._failed:
            return
        self._failed = True
        self._closed = True
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()
        with contextlib.suppress(Exception):
            await self._on_terminal_failure(self.local_session_id, error)

    async def _default_websocket_connect(self, url: str, headers: dict[str, str]) -> Any:
        return await websockets.connect(
            url,
            additional_headers=headers,
            max_size=MAX_SIDEBAND_FRAME_BYTES,
        )
