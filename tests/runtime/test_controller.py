from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from realtime_action_spike.openai.interruption import (
    InterruptionEvent,
    InterruptionEventKind,
)
from realtime_action_spike.runtime.browser import BrowserHandle, NoopBrowserHandle
from realtime_action_spike.runtime.controller import (
    SessionOutcome,
    StaleControlMessage,
    TerminalSessionResult,
    VoiceSessionController,
)
from realtime_action_spike.runtime.protocol import (
    PageReadyMessage,
    PageStartedMessage,
    SessionClosedMessage,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    TimingMessage,
    TimingName,
    encode_loopback_message,
)
from realtime_action_spike.runtime.tokens import LaunchTokenStore


class FakeBrowserHandle(NoopBrowserHandle):
    pass


class DeterministicLauncher:
    def __init__(self) -> None:
        self.launched_urls: list[str] = []
        self.handles: list[NoopBrowserHandle] = []

    def launch(self, loopback_url: str) -> BrowserHandle:
        self.launched_urls.append(loopback_url)
        handle = FakeBrowserHandle()
        self.handles.append(handle)
        return handle


class FailingLauncher:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def launch(self, loopback_url: str) -> BrowserHandle:
        self.calls += 1
        raise self.error


class FailingCloseHandle(NoopBrowserHandle):
    def close(self) -> None:
        self.closed_calls += 1
        raise RuntimeError("injected browser close failure")


class FirstCloseFailsLauncher(DeterministicLauncher):
    def launch(self, loopback_url: str) -> BrowserHandle:
        self.launched_urls.append(loopback_url)
        handle: FakeBrowserHandle | FailingCloseHandle = (
            FailingCloseHandle() if not self.handles else FakeBrowserHandle()
        )
        self.handles.append(handle)
        return handle


class LockReleasingLauncher(DeterministicLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.release_calls = 0

    def release_profile_lock(self) -> None:
        self.release_calls += 1


class SequenceFactory:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        value = self._values[self._index]
        self._index += 1
        return value


def interruption_controller() -> VoiceSessionController:
    return VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-interruption"])),
        session_id_factory=SequenceFactory(["local-interruption-01"]),
    )


@pytest.mark.asyncio
async def test_activate_is_single_session_under_concurrency() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2", "token-3"]),
        ),
        session_id_factory=SequenceFactory(
            ["local-session-01", "local-session-02", "local-session-03"]
        ),
    )

    statuses = await asyncio.gather(
        *(controller.activate("http://127.0.0.1:8765/voice") for _ in range(3))
    )

    opened = [status for status in statuses if status.status == "opened"]
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_busy_activation_is_reported_and_launcher_not_called_for_second_request() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01", "local-session-02"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    second = await controller.activate("http://127.0.0.1:8765/voice")

    assert first.status == "opened"
    assert first.session_id == "local-session-01"
    assert second.status == "busy"
    assert len(launcher.handles) == 1


@pytest.mark.asyncio
async def test_failed_activation_invalidates_token_and_releases_slot() -> None:
    launcher = FailingLauncher(RuntimeError("launch failed"))
    token_factory = SequenceFactory(["token-fail"])
    session_factory = SequenceFactory(["session-fail"])
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=token_factory),
        session_id_factory=session_factory,
    )

    result = await controller.activate("http://127.0.0.1:8765/voice")
    assert result.status == "failed"
    assert result.session_id == "session-fail"
    assert result.token == "token-fail"
    assert controller.token_store.validate("token-fail") is None
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_control_message_flow_tracks_state_and_idempotent_stop_teardown() -> None:
    launcher = DeterministicLauncher()
    observed_statuses: list[str] = []
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01"]),
        status_observer=observed_statuses.append,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    assert controller.status == "launching"

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    assert await controller.process_control_message(session_id, ready) is None
    assert controller.status == "connecting"

    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    assert await controller.process_control_message(session_id, started) is None
    assert controller.status == "live"

    marker = encode_loopback_message(
        TimingMessage(
            type="timing",
            session_id=session_id,
            name=TimingName.PEER_CONNECTION_STATE,
            monotonic_ms=12.0,
            data={"state": "connected"},
        )
    )
    assert await controller.process_control_message(session_id, marker) is None

    stop = encode_loopback_message(
        StopMessage(type="stop", session_id=session_id, reason=StopReason.BUTTON)
    )
    assert await controller.process_control_message(session_id, stop) is None
    assert controller.status == "stopping"
    assert await controller.process_control_message(session_id, stop) is None

    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    closed_first = await controller.process_control_message(session_id, teardown)
    closed_second = await controller.process_control_message(session_id, teardown)

    assert isinstance(closed_first, SessionClosedMessage)
    assert isinstance(closed_second, SessionClosedMessage)
    assert closed_first.session_id == session_id
    assert closed_second.session_id == session_id
    assert closed_first.outcome == closed_second.outcome
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"
    assert observed_statuses == ["launching", "connecting", "live", "stopping", "idle"]


@pytest.mark.asyncio
async def test_teardown_releases_profile_lock_after_browser_close() -> None:
    launcher = LockReleasingLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-lock"])),
        session_id_factory=SequenceFactory(["local-session-lock"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    # Any terminal path (button, X-close, native cancel) converges here.
    await controller.close_active_session()

    assert controller.status == "idle"
    assert launcher.handles[0].closed_calls == 1
    # The profile lock is released exactly once, after the browser was closed.
    assert launcher.release_calls == 1


@pytest.mark.asyncio
async def test_realtime_diagnostics_are_logged_while_session_is_active(caplog) -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-log"])),
        session_id_factory=SequenceFactory(["local-session-log"]),
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    caplog.set_level("INFO", logger="realtime_action_spike.runtime.controller")

    marker = encode_loopback_message(
        TimingMessage(
            type="timing",
            session_id=session_id,
            name=TimingName.REALTIME_ERROR,
            monotonic_ms=12.0,
            data={"error_type": "provider_error", "message": "response failed"},
        )
    )
    assert await controller.process_control_message(session_id, marker) is None

    diagnostic_records = [
        record.message for record in caplog.records if "session_diagnostic" in record.message
    ]
    assert len(diagnostic_records) == 1
    assert '"name":"realtime_error"' in diagnostic_records[0]
    assert '"message":"response failed"' in diagnostic_records[0]
    assert "token-log" not in diagnostic_records[0]
    await controller.close_active_session()


@pytest.mark.asyncio
async def test_stale_session_messages_are_rejected_after_newer_session_starts() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-a", "token-b"]),
        ),
        session_id_factory=SequenceFactory(["local-session-aa", "local-session-bb"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    first_session_id = first.session_id
    assert first_session_id is not None

    started = encode_loopback_message(
        PageReadyMessage(type="page_ready", session_id=first_session_id)
    )
    live = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=first_session_id)
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=first_session_id)
    )

    assert await controller.process_control_message(first_session_id, started) is None
    assert await controller.process_control_message(first_session_id, live) is None
    assert isinstance(
        await controller.process_control_message(first_session_id, teardown),
        SessionClosedMessage,
    )

    second = await controller.activate("http://127.0.0.1:8765/voice")
    second_session_id = second.session_id
    assert second_session_id == "local-session-bb"

    with pytest.raises(StaleControlMessage):
        await controller.process_control_message(
            first_session_id,
            encode_loopback_message(
                StopMessage(
                    type="stop",
                    session_id=first_session_id,
                    reason=StopReason.BUTTON,
                )
            ),
        )

    assert second_session_id is not None
    assert controller.active_session_id == second_session_id
    assert second_session_id == "local-session-bb"
    assert launcher.handles[0].closed_calls == 1
    assert launcher.handles[1].closed_calls == 0


@pytest.mark.asyncio
async def test_activation_creates_distinct_session_id_and_launch_token() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-distinct"]),
        ),
        session_id_factory=SequenceFactory(["session-distinct"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")

    assert activation.status == "opened"
    assert activation.session_id == "session-distinct"
    assert activation.token == "token-distinct"
    assert activation.token != activation.session_id


@pytest.mark.asyncio
async def test_wait_for_terminal_result_returns_completed_outcome() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-terminal-1"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    waiter = asyncio.create_task(controller.wait_for_terminal_result(session_id))

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    assert await controller.process_control_message(session_id, ready) is None
    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    await controller.process_control_message(session_id, started)
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    await controller.process_control_message(session_id, teardown)

    result = await waiter
    assert result == TerminalSessionResult(
        session_id=session_id,
        outcome=SessionOutcome.COMPLETED,
        error=None,
    )


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_terminal_future() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-terminal-2"])),
        session_id_factory=SequenceFactory(["local-session-02"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    waiter = asyncio.create_task(controller.wait_for_terminal_result(session_id))
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    ready = encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id))
    await controller.process_control_message(session_id, ready)
    started = encode_loopback_message(
        PageStartedMessage(type="page_started", session_id=session_id)
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    stop_msg = encode_loopback_message(
        StopMessage(type="stop", session_id=session_id, reason=StopReason.TRANSPORT_FAILURE)
    )
    await controller.process_control_message(session_id, started)
    await controller.process_control_message(session_id, stop_msg)
    await controller.process_control_message(session_id, teardown)

    result = await controller.wait_for_terminal_result(session_id)
    assert result.outcome == SessionOutcome.FAILED


@pytest.mark.asyncio
async def test_browser_startup_timeout_closes_browser_and_releases_slot() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-startup-timeout"]),
        ),
        session_id_factory=SequenceFactory(["session-startup-timeout"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    result = await asyncio.wait_for(
        controller.wait_for_terminal_result(activation.session_id),
        timeout=0.5,
    )

    assert result == TerminalSessionResult(
        session_id="session-startup-timeout",
        outcome=SessionOutcome.TIMED_OUT,
        error="browser startup timed out",
    )
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_page_started_cancels_browser_startup_timeout() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-startup-live"]),
        ),
        session_id_factory=SequenceFactory(["session-startup-live"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageReadyMessage(type="page_ready", session_id=activation.session_id)
        ),
    )
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=activation.session_id)
        ),
    )

    await asyncio.sleep(0.03)

    assert controller.status == "live"
    assert launcher.handles[0].closed_calls == 0
    await controller.close_active_session()
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_page_started_before_page_ready_is_accepted_and_cancels_startup_timeout() -> None:
    """The browser may observe session.updated before its page_ready handshake.

    The Realtime session can acknowledge over the data channel before the
    control websocket finishes its page_ready turn (e.g. fast reconnect, or
    the provider answering session.updated ahead of the loopback handshake).
    The controller must still accept page_started and cancel the startup
    deadline; rejecting it would reintroduce the 20s "activation failed"
    timeout family on an otherwise healthy session.
    """
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-started-first"]),
        ),
        session_id_factory=SequenceFactory(["session-started-first"]),
        browser_start_timeout_seconds=0.01,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.05,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    # page_started arrives while the session is still in LAUNCHING (no page_ready).
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=activation.session_id)
        ),
    )

    await asyncio.sleep(0.03)

    assert controller.status == "live"
    assert launcher.handles[0].closed_calls == 0
    await controller.close_active_session()
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_stuck_at_connecting_expires_as_timed_out_and_teardown_is_bounded() -> None:
    """Regression replay of the observed stuck-at-connecting failure family.

    Real trace pattern (e.g. Z2DkHx8cuaq0GAFyJA0odA): the browser connects the
    control socket, sends page_ready, creates the SDP offer and applies the
    answer, then the peer connection stalls at "connecting" — page_started
    never arrives because session.updated is never acknowledged. The startup
    deadline must expire the session as timed_out, teardown must complete in
    bounded time even though the browser never acknowledges the stop, and the
    controller must return to idle so the wakeword listener can re-arm.
    """
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-stuck", "token-follow-up"]),
        ),
        session_id_factory=SequenceFactory(["session-stuck", "session-follow-up"]),
        browser_start_timeout_seconds=0.02,
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.02,
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id is not None

    # Browser reaches CONNECTING (page_ready) and traces the observed events:
    # SDP offer, answer, peer stuck at "connecting" — but never page_started.
    await controller.process_control_message(
        activation.session_id,
        encode_loopback_message(
            PageReadyMessage(type="page_ready", session_id=activation.session_id)
        ),
    )
    for name, data in (
        (TimingName.PEER_CONNECTION_STATE, {"state": "connecting"}),
    ):
        await controller.process_control_message(
            activation.session_id,
            encode_loopback_message(
                TimingMessage(
                    type="timing",
                    session_id=activation.session_id,
                    name=name,
                    monotonic_ms=1.0,
                    data=data,
                )
            ),
        )

    terminal = await asyncio.wait_for(
        controller.wait_for_terminal_result(activation.session_id),
        timeout=2.0,
    )

    assert terminal.outcome == SessionOutcome.TIMED_OUT
    assert controller.status == "idle"
    assert launcher.handles[0].closed_calls >= 1

    # The session slot must be free for the next wake activation.
    follow_up = await controller.activate("http://127.0.0.1:8765/voice")
    assert follow_up.status == "opened"
    await controller.close_active_session()


@pytest.mark.asyncio
async def test_launch_failure_resolves_terminal_result_and_releases_slot() -> None:
    launcher = FailingLauncher(RuntimeError("launch failed"))
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-fail-2"])),
        session_id_factory=SequenceFactory(["session-fail-2"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.status == "failed"

    result = await controller.wait_for_terminal_result(activation.session_id or "")
    assert result == TerminalSessionResult(
        session_id="session-fail-2",
        outcome=SessionOutcome.FAILED,
        error="activation failed",
    )
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_stale_control_message_cannot_resolve_newer_session_terminal_future() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1", "token-2"]),
        ),
        session_id_factory=SequenceFactory(["session-first-01", "session-second-02"]),
    )

    first = await controller.activate("http://127.0.0.1:8765/voice")
    first_id = first.session_id or ""

    await controller.process_control_message(
        first_id,
        encode_loopback_message(PageReadyMessage(type="page_ready", session_id=first_id)),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=first_id)
        ),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            StopMessage(type="stop", session_id=first_id, reason=StopReason.BUTTON)
        ),
    )
    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=first_id)
        ),
    )

    first_result = await controller.wait_for_terminal_result(first_id)
    assert first_result.outcome == SessionOutcome.COMPLETED

    stale_stop = encode_loopback_message(
        StopMessage(type="stop", session_id=first_id, reason=StopReason.BUTTON)
    )
    with pytest.raises(StaleControlMessage):
        await controller.process_control_message(first_id, stale_stop)

    second = await controller.activate("http://127.0.0.1:8765/voice")
    second_id = second.session_id or ""
    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            PageReadyMessage(type="page_ready", session_id=second_id)
        ),
    )

    second_waiter = asyncio.create_task(controller.wait_for_terminal_result(second_id))

    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            PageStartedMessage(type="page_started", session_id=second_id)
        ),
    )
    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            StopMessage(type="stop", session_id=second_id, reason=StopReason.BUTTON)
        ),
    )
    await controller.process_control_message(
        second_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=second_id)
        ),
    )

    result = await second_waiter
    assert result.outcome == SessionOutcome.COMPLETED


@pytest.mark.asyncio
async def test_controller_owns_interruption_reducer_for_exact_active_session() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id == "local-interruption-01"

    await controller.begin_realtime_response(
        session_id,
        "resp-1",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )
    accepted = await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
            user_speech_onset_ms=30.0,
        ),
    )

    traces = await controller.interruption_traces(session_id)
    assert accepted is True
    assert len(traces) == 1
    assert traces[0].response_id == "resp-1"
    assert traces[0].provider_audio_start_ms == 25
    assert traces[0].speech_started_received_ns == 2_000


@pytest.mark.asyncio
async def test_controller_rejects_stale_session_and_response_interruption_events() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    await controller.begin_realtime_response(
        session_id,
        "resp-current",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )

    with pytest.raises(StaleControlMessage):
        await controller.record_interruption_event(
            "stale-session-000",
            InterruptionEvent(
                local_session_id="stale-session-000",
                response_id="resp-current",
                kind=InterruptionEventKind.SPEECH_STARTED,
                occurred_ns=2_000,
            ),
        )

    accepted = await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-stale",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
        ),
    )
    assert accepted is False
    assert await controller.interruption_traces(session_id) == ()


@pytest.mark.asyncio
async def test_controller_retains_completed_session_interruption_traces() -> None:
    controller = interruption_controller()
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None
    await controller.begin_realtime_response(
        session_id,
        "resp-1",
        received_ns=1_000,
        provider_audio_start_ms=25,
    )
    await controller.record_interruption_event(
        session_id,
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=2_000,
        ),
    )
    teardown = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
    )
    await controller.process_control_message(session_id, teardown)

    traces = await controller.interruption_traces(session_id)
    assert len(traces) == 1
    assert traces[0].response_id == "resp-1"


@pytest.mark.asyncio
async def test_server_teardown_requests_browser_stop_then_waits_for_ack() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-server-stop"]),
        ),
        session_id_factory=SequenceFactory(["local-server-stop"]),
        browser_ack_timeout_seconds=0.2,
        teardown_step_timeout_seconds=0.2,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    teardown_task = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.FAILED,
            reason=StopReason.TRANSPORT_FAILURE,
            error="transport failed",
        )
    )
    outbound = await asyncio.wait_for(
        controller.wait_for_outbound_message(session_id),
        timeout=0.1,
    )

    assert outbound == StopMessage(
        type="stop",
        session_id=session_id,
        reason=StopReason.TRANSPORT_FAILURE,
    )
    assert controller.status == "stopping"
    assert not teardown_task.done()

    closed = await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )
    report = await teardown_task
    result = await controller.wait_for_terminal_result(session_id)

    assert isinstance(closed, SessionClosedMessage)
    assert report.request.outcome is SessionOutcome.FAILED
    assert report.browser_acknowledged is True
    assert result == TerminalSessionResult(
        session_id=session_id,
        outcome=SessionOutcome.FAILED,
        error="transport failed",
    )
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_duplicate_teardown_uses_first_terminal_outcome_and_closes_once() -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-duplicate-stop"])),
        session_id_factory=SequenceFactory(["local-duplicate-stop"]),
        browser_ack_timeout_seconds=0.2,
        teardown_step_timeout_seconds=0.2,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    first = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.TIMED_OUT,
            reason=StopReason.TIMEOUT,
        )
    )
    outbound = await asyncio.wait_for(controller.wait_for_outbound_message(session_id), 0.1)
    second = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        )
    )
    await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )

    first_report, second_report = await asyncio.gather(first, second)
    result = await controller.wait_for_terminal_result(session_id)

    assert isinstance(outbound, StopMessage)
    assert first_report is second_report
    assert first_report.request.outcome is SessionOutcome.TIMED_OUT
    assert result.outcome is SessionOutcome.TIMED_OUT
    assert launcher.handles[0].closed_calls == 1


@pytest.mark.asyncio
async def test_missing_browser_ack_is_bounded_and_persists_final_trace_atomically(
    tmp_path: Path,
) -> None:
    launcher = DeterministicLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-no-ack"])),
        session_id_factory=SequenceFactory(["local-no-browser-ack"]),
        browser_ack_timeout_seconds=0.01,
        teardown_step_timeout_seconds=0.2,
        trace_directory=tmp_path,
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    report = await asyncio.wait_for(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        ),
        timeout=0.2,
    )

    assert [(failure.step, failure.kind) for failure in report.failures] == [
        ("browser_ack", "timeout")
    ]
    assert launcher.handles[0].closed_calls == 1
    assert controller.status == "idle"
    trace_path = tmp_path / f"{session_id}.jsonl"
    assert trace_path.is_file()
    assert "teardown_requested" in trace_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(f".{trace_path.name}.*.tmp"))


@pytest.mark.asyncio
async def test_browser_close_failure_still_returns_idle_and_rearms() -> None:
    launcher = FirstCloseFailsLauncher()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-close-fails", "token-rearmed"])
        ),
        session_id_factory=SequenceFactory(["local-close-fails", "local-rearmed-02"]),
        browser_ack_timeout_seconds=0.2,
    )
    first = await controller.activate("http://127.0.0.1:8765/voice")
    assert first.session_id is not None

    teardown = asyncio.create_task(
        controller.request_teardown(
            first.session_id,
            outcome=SessionOutcome.CANCELLED,
            reason=StopReason.NATIVE_CANCEL,
        )
    )
    await controller.wait_for_outbound_message(first.session_id)
    await controller.process_control_message(
        first.session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=first.session_id)
        ),
    )
    report = await teardown

    assert [(failure.step, failure.kind) for failure in report.failures] == [
        ("close_browser", "error")
    ]
    assert controller.status == "idle"
    second = await controller.activate("http://127.0.0.1:8765/voice")
    assert second.status == "opened"
    assert second.session_id == "local-rearmed-02"

    await controller.close_active_session()
