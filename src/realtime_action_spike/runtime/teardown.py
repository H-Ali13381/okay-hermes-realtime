from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .protocol import SessionOutcome, StopReason


@dataclass(frozen=True, slots=True)
class TeardownRequest:
    outcome: SessionOutcome
    reason: StopReason
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TeardownStepFailure:
    step: str
    kind: Literal["error", "timeout"]


@dataclass(frozen=True, slots=True)
class TeardownReport:
    request: TeardownRequest
    browser_acknowledged: bool
    failures: tuple[TeardownStepFailure, ...]


async def _noop_release_profile_lock() -> None:
    """Default profile-lock release for browsers without a dedicated lock."""
    return None


@dataclass(frozen=True, slots=True)
class TeardownHooks:
    mark_stopping: Callable[[TeardownRequest], Awaitable[None]]
    request_browser_stop: Callable[[TeardownRequest], Awaitable[None]]
    close_sideband: Callable[[], Awaitable[None]]
    close_browser: Callable[[], Awaitable[None]]
    persist_trace: Callable[[tuple[TeardownStepFailure, ...]], Awaitable[None]]
    finalize: Callable[
        [TeardownRequest, tuple[TeardownStepFailure, ...]],
        Awaitable[None],
    ]
    release_profile_lock: Callable[[], Awaitable[None]] = _noop_release_profile_lock


class TeardownCoordinator:
    """Run one bounded teardown sequence regardless of duplicate callers."""

    def __init__(
        self,
        hooks: TeardownHooks,
        *,
        acknowledgement_timeout: float = 1.0,
        step_timeout: float = 2.0,
    ) -> None:
        self._validate_timeout("acknowledgement_timeout", acknowledgement_timeout)
        self._validate_timeout("step_timeout", step_timeout)
        self._hooks = hooks
        self._acknowledgement_timeout = acknowledgement_timeout
        self._step_timeout = step_timeout
        self._browser_acknowledged = asyncio.Event()
        self._task: asyncio.Task[TeardownReport] | None = None

    @staticmethod
    def _validate_timeout(name: str, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a finite positive number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite positive number")

    def acknowledge_browser_teardown(self) -> None:
        self._browser_acknowledged.set()

    def start(self, request: TeardownRequest) -> asyncio.Task[TeardownReport]:
        """Start synchronously so the first event-loop caller owns the outcome."""

        if self._task is None:
            self._task = asyncio.create_task(self._run(request))
        return self._task

    async def run(
        self,
        request: TeardownRequest,
        *,
        shield: bool = True,
    ) -> TeardownReport:
        task = self.start(request)
        if shield:
            return await asyncio.shield(task)
        return await task

    async def _run(self, request: TeardownRequest) -> TeardownReport:
        failures: list[TeardownStepFailure] = []
        browser_acknowledged = False
        finalization_task: asyncio.Task[bool] | None = None

        try:
            await self._run_step(
                "mark_stopping",
                lambda: self._hooks.mark_stopping(request),
                failures,
            )
            browser_stop_requested = await self._run_step(
                "request_browser_stop",
                lambda: self._hooks.request_browser_stop(request),
                failures,
            )

            if browser_stop_requested:
                try:
                    await asyncio.wait_for(
                        self._browser_acknowledged.wait(),
                        timeout=self._acknowledgement_timeout,
                    )
                    browser_acknowledged = True
                except TimeoutError:
                    failures.append(TeardownStepFailure(step="browser_ack", kind="timeout"))

            await self._run_step("close_sideband", self._hooks.close_sideband, failures)
            await self._run_step("close_browser", self._hooks.close_browser, failures)
            await self._run_step(
                "release_profile_lock",
                self._hooks.release_profile_lock,
                failures,
            )
            await self._run_step(
                "persist_trace",
                lambda: self._hooks.persist_trace(tuple(failures)),
                failures,
            )
            finalization_task = asyncio.create_task(
                self._run_step(
                    "finalize",
                    lambda: self._hooks.finalize(request, tuple(failures)),
                    failures,
                )
            )
            await asyncio.shield(finalization_task)
        except asyncio.CancelledError:
            failures.append(TeardownStepFailure(step="teardown_cancelled", kind="error"))
            if finalization_task is None:
                finalization_task = asyncio.create_task(
                    self._run_step(
                        "finalize",
                        lambda: self._hooks.finalize(request, tuple(failures)),
                        failures,
                    )
                )
            await asyncio.shield(finalization_task)
            raise

        return TeardownReport(
            request=request,
            browser_acknowledged=browser_acknowledged,
            failures=tuple(failures),
        )

    async def _run_step(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
        failures: list[TeardownStepFailure],
    ) -> bool:
        try:
            await asyncio.wait_for(callback(), timeout=self._step_timeout)
        except TimeoutError:
            failures.append(TeardownStepFailure(step=name, kind="timeout"))
            return False
        except Exception:
            failures.append(TeardownStepFailure(step=name, kind="error"))
            return False
        return True
