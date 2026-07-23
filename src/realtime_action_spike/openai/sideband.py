from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import websockets

JsonObject = Mapping[str, Any]
MAX_SIDEBAND_EVENT_BYTES = 256_000
# Hard ceiling on a single raw frame before parsing. OpenAI can embed the full
# input audio (~1MB base64) in retrieved conversation items; we allow the frame
# in, strip the audio we do not use, then apply MAX_SIDEBAND_EVENT_BYTES.
MAX_SIDEBAND_FRAME_BYTES = 4_000_000

logger = logging.getLogger(__name__)

# TEMPORARY DIAGNOSTIC (2026-07): full firehose logging of the sideband event
# stream to hunt an intermittent ValueError that fails otherwise-healthy
# sessions. This logs raw provider events UNREDACTED (transcript text, tool
# arguments) by explicit user request. Strip back to the bare minimum once the
# ValueError is identified. See conversation + realtime-api-verification pitfall
# #15 (retain raw events before narrow projections).
_DIAGNOSTIC_FIREHOSE = True
_DIAGNOSTIC_RING_SIZE = 40


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

        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._closed = False
        self._failed = False
        # TEMPORARY DIAGNOSTIC: bounded raw-event ring for post-failure dumps.
        self._diagnostic_ring: collections.deque[str] = collections.deque(
            maxlen=_DIAGNOSTIC_RING_SIZE
        )

    @property
    def closed(self) -> bool:
        return self._closed

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("sideband client already closed")
            if self._websocket is not None:
                return

            headers = {"Authorization": f"Bearer {self._api_key}"}
            try:
                websocket = await self._connect(
                    build_sideband_url(self.call_id),
                    headers,
                )
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

        if _DIAGNOSTIC_FIREHOSE:
            logger.warning(
                "sideband_tx session_id=%s event=%s",
                self.local_session_id,
                data,
            )

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
        websocket = self._websocket
        if websocket is None:
            return

        raw_message: Any = None
        try:
            while True:
                raw_message = await websocket.recv()
                if _DIAGNOSTIC_FIREHOSE:
                    ring_text = (
                        raw_message.decode("utf-8", "replace")
                        if isinstance(raw_message, bytes)
                        else str(raw_message)
                    )
                    self._diagnostic_ring.append(ring_text)
                    logger.warning(
                        "sideband_rx session_id=%s event=%s",
                        self.local_session_id,
                        ring_text,
                    )
                message = self._decode_message(raw_message)
                await self._on_event(
                    SidebandEvent(local_session_id=self.local_session_id, payload=message)
                )
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._log_reader_failure(error, raw_message)
            await self._fail(error)

    def _log_reader_failure(self, error: Exception, raw_message: Any) -> None:
        # TEMPORARY DIAGNOSTIC: dump the offending event, the full traceback, and
        # the recent raw-event ring so an intermittent ValueError is fully
        # attributable. Strip once the root cause is fixed.
        if not _DIAGNOSTIC_FIREHOSE:
            return
        offending = (
            raw_message.decode("utf-8", "replace")
            if isinstance(raw_message, bytes)
            else repr(raw_message)
        )
        formatted_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        logger.error(
            "sideband_reader_failure session_id=%s error_type=%s error=%s\n"
            "offending_event=%s\ntraceback=\n%s\nrecent_events=\n%s",
            self.local_session_id,
            type(error).__name__,
            error,
            offending,
            formatted_traceback,
            "\n".join(self._diagnostic_ring),
        )

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
        return await websockets.connect(url, additional_headers=headers)
