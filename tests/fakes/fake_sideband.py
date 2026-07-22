from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSidebandWebSocket:
    messages: deque[str | BaseException] = field(default_factory=deque)
    sent: list[str] = field(default_factory=list)
    closed: bool = False

    def __post_init__(self) -> None:
        self._message_ready = asyncio.Event()
        if self.messages:
            self._message_ready.set()

    def push(self, payload: dict[str, Any] | BaseException) -> None:
        if isinstance(payload, BaseException):
            self.messages.append(payload)
        else:
            self.messages.append(json.dumps(payload))
        self._message_ready.set()

    async def recv(self) -> str:
        while not self.messages:
            if self.closed:
                raise RuntimeError("sideband closed")
            self._message_ready.clear()
            await self._message_ready.wait()
        message = self.messages.popleft()
        if not self.messages:
            self._message_ready.clear()
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        self._message_ready.set()


class FakeSidebandConnector:
    def __init__(self, websocket: FakeSidebandWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeSidebandWebSocket:
        self.calls.append((url, headers))
        return self.websocket
