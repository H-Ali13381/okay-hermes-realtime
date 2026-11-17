from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Literal, Protocol

from .cli import run_login_broker
from .contracts import RemotePrototypeError


class LoginRunner(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        brave_binary: str,
        profile_path: str,
        timeout_seconds: float,
        ready: asyncio.Event,
    ) -> Coroutine[Any, Any, None]: ...


class CrunchyrollLoginManager:
    """Own one exact-session Crunchyroll login broker task."""

    def __init__(
        self,
        *,
        brave_binary: str,
        profile_path: str | Path,
        timeout_seconds: float = 300.0,
        startup_timeout_seconds: float = 10.0,
        runner: LoginRunner = run_login_broker,
    ) -> None:
        self._brave_binary = brave_binary
        self._profile_path = str(Path(profile_path).expanduser())
        self._timeout_seconds = timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._runner = runner
        self._lock = asyncio.Lock()
        self._active_session_id: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def active_session_id(self) -> str | None:
        task = self._task
        if task is None or task.done():
            return None
        return self._active_session_id

    async def start(
        self,
        *,
        session_id: str,
        query: str,
        episode: int | None,
        audio_mode: Literal["auto", "sub", "dub"],
    ) -> dict[str, object]:
        del episode, audio_mode
        ready = asyncio.Event()
        async with self._lock:
            existing = self.active_session_id
            if existing == session_id:
                raise ValueError("Crunchyroll login is already active for this voice session")
            if existing is not None:
                raise ValueError("Crunchyroll login belongs to another voice session")
            task = asyncio.create_task(
                self._runner(
                    session_id=session_id,
                    brave_binary=self._brave_binary,
                    profile_path=self._profile_path,
                    timeout_seconds=self._timeout_seconds,
                    ready=ready,
                ),
                name=f"crunchyroll-login:{session_id}",
            )
            self._active_session_id = session_id
            self._task = task
            task.add_done_callback(self._clear_completed_task)

        ready_waiter = asyncio.create_task(ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, ready_waiter},
                timeout=self._startup_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                try:
                    await task
                except RemotePrototypeError:
                    raise
                except Exception:
                    raise RemotePrototypeError("Crunchyroll login could not start") from None
                raise RemotePrototypeError("Crunchyroll login stopped during startup")
            if ready_waiter not in done:
                await self.stop_session(session_id)
                raise RemotePrototypeError("Crunchyroll login did not open in time")
        finally:
            ready_waiter.cancel()

        return {
            "status": "authentication_required",
            "provider": "crunchyroll",
            "login_window_opened": True,
            "request_query": query,
            "message": "Scan the QR code in the Crunchyroll window to sign in.",
        }

    async def stop_session(self, session_id: str) -> None:
        async with self._lock:
            if self._active_session_id != session_id:
                return
            task = self._task
            self._active_session_id = None
            self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        async with self._lock:
            task = self._task
            self._active_session_id = None
            self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    def _clear_completed_task(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None
            self._active_session_id = None
        if not task.cancelled():
            task.exception()
