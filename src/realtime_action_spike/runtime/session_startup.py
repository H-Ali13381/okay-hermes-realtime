from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

OnStartupExpired = Callable[[], Awaitable[None]]


class BrowserStartupDeadline:
    """Own the cancellable deadline between browser launch and page startup."""

    def __init__(self, timeout_seconds: float, on_expired: OnStartupExpired) -> None:
        self._timeout_seconds = timeout_seconds
        self._on_expired = on_expired
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("browser startup deadline already started")
        self._task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._timeout_seconds)
        except asyncio.CancelledError:
            return

        self._task = None
        await self._on_expired()
