from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from realtime_action_spike.runtime.protocol import SessionOutcome, StopReason
from realtime_action_spike.runtime.teardown import (
    TeardownCoordinator,
    TeardownHooks,
    TeardownRequest,
)


@dataclass
class HookRecorder:
    events: list[str] = field(default_factory=list)
    fail_step: str | None = None
    coordinator: TeardownCoordinator | None = None

    async def _record(self, name: str) -> None:
        self.events.append(name)
        if self.fail_step == name:
            raise RuntimeError(f"{name} failed")

    async def mark_stopping(self, _request: TeardownRequest) -> None:
        await self._record("mark_stopping")

    async def request_browser_stop(self, _request: TeardownRequest) -> None:
        await self._record("request_browser_stop")
        if self.coordinator is not None and self.fail_step != "request_browser_stop":
            self.coordinator.acknowledge_browser_teardown()

    async def close_sideband(self) -> None:
        await self._record("close_sideband")

    async def close_browser(self) -> None:
        await self._record("close_browser")

    async def release_profile_lock(self) -> None:
        await self._record("release_profile_lock")

    async def persist_trace(self, _failures: tuple[object, ...]) -> None:
        await self._record("persist_trace")

    async def finalize(
        self,
        _request: TeardownRequest,
        _failures: tuple[object, ...],
    ) -> None:
        await self._record("finalize")

    def hooks(self) -> TeardownHooks:
        return TeardownHooks(
            mark_stopping=self.mark_stopping,
            request_browser_stop=self.request_browser_stop,
            close_sideband=self.close_sideband,
            close_browser=self.close_browser,
            persist_trace=self.persist_trace,
            finalize=self.finalize,
            release_profile_lock=self.release_profile_lock,
        )


@pytest.mark.asyncio
async def test_teardown_runs_the_required_order_and_records_acknowledgement() -> None:
    recorder = HookRecorder()
    coordinator = TeardownCoordinator(recorder.hooks(), acknowledgement_timeout=0.1)
    recorder.coordinator = coordinator
    request = TeardownRequest(
        outcome=SessionOutcome.COMPLETED,
        reason=StopReason.BUTTON,
    )

    report = await coordinator.run(request)

    assert recorder.events == [
        "mark_stopping",
        "request_browser_stop",
        "close_sideband",
        "close_browser",
        "release_profile_lock",
        "persist_trace",
        "finalize",
    ]
    assert report.request == request
    assert report.browser_acknowledged is True
    assert report.failures == ()


@pytest.mark.asyncio
async def test_duplicate_teardown_runs_once_and_first_request_wins() -> None:
    recorder = HookRecorder()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_browser_stop(_request: TeardownRequest) -> None:
        recorder.events.append("request_browser_stop")
        entered.set()
        await release.wait()
        assert recorder.coordinator is not None
        recorder.coordinator.acknowledge_browser_teardown()

    hooks = recorder.hooks()
    hooks = TeardownHooks(
        mark_stopping=hooks.mark_stopping,
        request_browser_stop=blocking_browser_stop,
        close_sideband=hooks.close_sideband,
        close_browser=hooks.close_browser,
        persist_trace=hooks.persist_trace,
        finalize=hooks.finalize,
    )
    coordinator = TeardownCoordinator(hooks, acknowledgement_timeout=0.1)
    recorder.coordinator = coordinator
    first_request = TeardownRequest(
        outcome=SessionOutcome.CANCELLED,
        reason=StopReason.NATIVE_CANCEL,
    )
    conflicting_request = TeardownRequest(
        outcome=SessionOutcome.FAILED,
        reason=StopReason.TRANSPORT_FAILURE,
        error="late failure",
    )

    first = asyncio.create_task(coordinator.run(first_request))
    await entered.wait()
    duplicate = asyncio.create_task(coordinator.run(conflicting_request))
    release.set()
    first_report, duplicate_report = await asyncio.gather(first, duplicate)

    assert first_report is duplicate_report
    assert first_report.request == first_request
    assert recorder.events.count("mark_stopping") == 1
    assert recorder.events.count("request_browser_stop") == 1
    assert recorder.events.count("finalize") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_step",
    [
        "mark_stopping",
        "request_browser_stop",
        "close_sideband",
        "close_browser",
        "release_profile_lock",
        "persist_trace",
        "finalize",
    ],
)
async def test_teardown_continues_to_finalize_after_each_step_failure(
    failed_step: str,
) -> None:
    recorder = HookRecorder(fail_step=failed_step)
    coordinator = TeardownCoordinator(
        recorder.hooks(),
        acknowledgement_timeout=0.01,
        step_timeout=0.1,
    )
    recorder.coordinator = coordinator

    report = await coordinator.run(
        TeardownRequest(
            outcome=SessionOutcome.FAILED,
            reason=StopReason.TRANSPORT_FAILURE,
            error="sanitized failure",
        )
    )

    assert recorder.events[-1] == "finalize"
    assert any(failure.step == failed_step for failure in report.failures)
    assert recorder.events.count("finalize") == 1


@pytest.mark.asyncio
async def test_missing_browser_ack_is_bounded_and_cleanup_continues() -> None:
    recorder = HookRecorder()

    async def no_ack(_request: TeardownRequest) -> None:
        recorder.events.append("request_browser_stop")

    hooks = recorder.hooks()
    coordinator = TeardownCoordinator(
        TeardownHooks(
            mark_stopping=hooks.mark_stopping,
            request_browser_stop=no_ack,
            close_sideband=hooks.close_sideband,
            close_browser=hooks.close_browser,
            persist_trace=hooks.persist_trace,
            finalize=hooks.finalize,
        ),
        acknowledgement_timeout=0.01,
        step_timeout=0.1,
    )

    report = await asyncio.wait_for(
        coordinator.run(
            TeardownRequest(
                outcome=SessionOutcome.TIMED_OUT,
                reason=StopReason.TIMEOUT,
            )
        ),
        timeout=0.5,
    )

    assert report.browser_acknowledged is False
    assert any(failure.step == "browser_ack" for failure in report.failures)
    # This coordinator uses the default no-op release_profile_lock, so it is
    # not recorded; the ordered cleanup tail is otherwise unchanged.
    assert recorder.events[-3:] == ["close_browser", "persist_trace", "finalize"]


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_shared_teardown() -> None:
    recorder = HookRecorder()
    browser_stop_started = asyncio.Event()

    async def wait_for_ack(_request: TeardownRequest) -> None:
        recorder.events.append("request_browser_stop")
        browser_stop_started.set()

    hooks = recorder.hooks()
    coordinator = TeardownCoordinator(
        TeardownHooks(
            mark_stopping=hooks.mark_stopping,
            request_browser_stop=wait_for_ack,
            close_sideband=hooks.close_sideband,
            close_browser=hooks.close_browser,
            persist_trace=hooks.persist_trace,
            finalize=hooks.finalize,
        ),
        acknowledgement_timeout=1.0,
        step_timeout=0.1,
    )
    request = TeardownRequest(
        outcome=SessionOutcome.CANCELLED,
        reason=StopReason.NATIVE_CANCEL,
    )

    caller = asyncio.create_task(coordinator.run(request))
    await browser_stop_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    coordinator.acknowledge_browser_teardown()
    report = await asyncio.wait_for(coordinator.run(request), timeout=0.5)

    assert report.request == request
    assert report.browser_acknowledged is True
    assert recorder.events.count("mark_stopping") == 1
    assert recorder.events.count("finalize") == 1


@pytest.mark.asyncio
async def test_unshielded_shutdown_cancellation_finalizes_and_leaves_no_task() -> None:
    recorder = HookRecorder()
    entered = asyncio.Event()

    async def blocked_sideband_close() -> None:
        recorder.events.append("close_sideband")
        entered.set()
        await asyncio.Event().wait()

    hooks = recorder.hooks()
    coordinator = TeardownCoordinator(
        TeardownHooks(
            mark_stopping=hooks.mark_stopping,
            request_browser_stop=hooks.request_browser_stop,
            close_sideband=blocked_sideband_close,
            close_browser=hooks.close_browser,
            persist_trace=hooks.persist_trace,
            finalize=hooks.finalize,
        ),
        acknowledgement_timeout=0.01,
        step_timeout=1.0,
    )
    recorder.coordinator = coordinator
    request = TeardownRequest(
        outcome=SessionOutcome.CANCELLED,
        reason=StopReason.NATIVE_CANCEL,
    )

    shutdown = asyncio.create_task(coordinator.run(request, shield=False))
    await asyncio.wait_for(entered.wait(), timeout=0.1)
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    shared_task = coordinator.start(request)
    assert shared_task.done()
    assert shared_task.cancelled()
    assert recorder.events.count("finalize") == 1
    assert "close_browser" not in recorder.events
    assert "persist_trace" not in recorder.events
