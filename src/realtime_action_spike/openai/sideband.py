from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import websockets

JsonObject = Mapping[str, Any]
MAX_SIDEBAND_EVENT_BYTES = 256_000


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
            await self._fail(error)

    def _decode_message(self, raw_message: Any) -> JsonObject:
        raw_text = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        if not isinstance(raw_text, str):
            raise TypeError("sideband message must be text")
        if len(raw_text.encode("utf-8")) > MAX_SIDEBAND_EVENT_BYTES:
            raise ValueError("sideband message is too large")

        message = json.loads(raw_text)
        if not isinstance(message, dict):
            raise TypeError("sideband message payload must be a JSON object")
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
