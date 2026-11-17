from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .contracts import RemoteCommand, RemotePrototypeError


class CdpTransport(Protocol):
    async def call(self, method: str, **params: Any) -> dict[str, Any]: ...


class LoopbackCdpTransport:
    """Lazy CDP client for the single app page owned by the prototype."""

    def __init__(
        self,
        *,
        debug_port: int,
        expected_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = f"http://127.0.0.1:{debug_port}"
        self._expected_url = expected_url
        self._timeout_seconds = timeout_seconds
        self._sequence = itertools.count(1)
        self._websocket: Any = None
        self._lock = asyncio.Lock()
        self._bootstrap_attempted = False

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        async with self._lock:
            try:
                if self._websocket is None:
                    self._websocket = await self._connect()
                request_id = next(self._sequence)
                await self._websocket.send(
                    json.dumps({"id": request_id, "method": method, "params": params})
                )
                while True:
                    raw = await asyncio.wait_for(self._websocket.recv(), self._timeout_seconds)
                    event = json.loads(raw)
                    if event.get("id") != request_id:
                        continue
                    if "error" in event:
                        raise RemotePrototypeError("Brave rejected a remote input command")
                    result = event.get("result", {})
                    return result if isinstance(result, dict) else {}
            except RemotePrototypeError:
                raise
            except (
                OSError,
                TimeoutError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                WebSocketException,
            ) as exc:
                await self._drop_websocket()
                raise RemotePrototypeError("Brave remote connection failed") from exc

    async def close(self) -> None:
        await self._drop_websocket()

    async def _drop_websocket(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close()

    async def _connect(self) -> Any:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        async with httpx.AsyncClient(timeout=1.0) as client:
            while True:
                try:
                    response = await client.get(f"{self._base_url}/json/list")
                    response.raise_for_status()
                    targets = response.json()
                    try:
                        websocket_url = _select_page_target(
                            targets,
                            expected_url=self._expected_url,
                        )
                    except StopIteration:
                        if self._bootstrap_attempted:
                            raise
                        bootstrap_url = _select_origin_bootstrap_target(targets)
                        self._bootstrap_attempted = True
                        await _navigate_bootstrap_target(
                            bootstrap_url,
                            expected_url=self._expected_url,
                        )
                        await asyncio.sleep(0.05)
                        continue
                    return await connect(
                        websocket_url,
                        open_timeout=2.0,
                        max_size=1_048_576,
                    )
                except (httpx.HTTPError, StopIteration, KeyError, OSError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RemotePrototypeError(
                            "Brave debugging endpoint did not become ready"
                        ) from None
                    await asyncio.sleep(0.05)


async def _navigate_bootstrap_target(websocket_url: str, *, expected_url: str) -> None:
    websocket = await connect(
        websocket_url,
        open_timeout=2.0,
        max_size=1_048_576,
    )
    try:
        await websocket.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Page.navigate",
                    "params": {"url": expected_url},
                }
            )
        )
        while True:
            raw = await asyncio.wait_for(websocket.recv(), 2.0)
            event = json.loads(raw)
            if event.get("id") != 1:
                continue
            if "error" in event:
                raise RemotePrototypeError("Brave rejected the owned host page")
            return
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


def _select_origin_bootstrap_target(targets: Any) -> str:
    if not isinstance(targets, list):
        raise StopIteration
    pages = [item for item in targets if isinstance(item, dict) and item.get("type") == "page"]
    if len(pages) != 1:
        raise StopIteration
    target = pages[0]
    if target.get("url") != "chrome://brave-origin-startup/" or not isinstance(
        target.get("webSocketDebuggerUrl"), str
    ):
        raise StopIteration
    return target["webSocketDebuggerUrl"]


def _select_page_target(targets: Any, *, expected_url: str) -> str:
    if not isinstance(targets, list):
        raise StopIteration
    target = next(
        item
        for item in targets
        if isinstance(item, dict)
        and item.get("type") == "page"
        and item.get("url") == expected_url
        and isinstance(item.get("webSocketDebuggerUrl"), str)
    )
    return target["webSocketDebuggerUrl"]


@dataclass(slots=True)
class CdpInputController:
    transport: CdpTransport
    paired: bool = False
    cancelled: asyncio.Event | None = None

    CRUNCHYROLL_SSO_URL = "https://sso.crunchyroll.com/login"

    async def apply(self, command: RemoteCommand) -> None:
        if command.kind == "pair":
            if self.paired:
                raise RemotePrototypeError("phone is already paired")
            await self.transport.call("Page.navigate", url=self.CRUNCHYROLL_SSO_URL)
            self.paired = True
            return
        if not self.paired:
            raise RemotePrototypeError("phone must be paired before sending input")
        if command.kind == "text":
            await self.transport.call("Input.insertText", text=command.value)
            return
        if command.kind == "key":
            await self._dispatch_key(command.value or "")
            return
        if command.kind == "cancel":
            if self.cancelled is not None:
                self.cancelled.set()
            return
        raise RemotePrototypeError("unsupported remote command")

    async def _dispatch_key(self, key: str) -> None:
        key_data = {
            "Tab": ("Tab", 9),
            "Enter": ("Enter", 13),
            "Backspace": ("Backspace", 8),
        }.get(key)
        if key_data is None:
            raise RemotePrototypeError("unsupported key")
        code, virtual_key = key_data
        for event_type in ("keyDown", "keyUp"):
            await self.transport.call(
                "Input.dispatchKeyEvent",
                type=event_type,
                key=key,
                code=code,
                windowsVirtualKeyCode=virtual_key,
            )
