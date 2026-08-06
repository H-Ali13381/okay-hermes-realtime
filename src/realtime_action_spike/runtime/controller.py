from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from realtime_action_spike.capabilities import CapabilityBroker

from .browser import BrowserHandle
from .protocol import (
    LoopbackMessage,
    PageReadyMessage,
    PageStartedMessage,
    SessionClosedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    TimingMessage,
    parse_loopback_message,
)
from .session_startup import BrowserStartupDeadline
from .session_state import SessionPhase, SessionState, SessionTransitionError
from .teardown import (
    TeardownCoordinator,
    TeardownHooks,
    TeardownReport,
    TeardownRequest,
    TeardownStepFailure,
)
from .timing import JsonValue, SessionTrace, sanitize_timing_data
from .tokens import LaunchTokenStore

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from realtime_action_spike.openai.interruption import (
        InterruptionEvent,
        InterruptionTimelineReducer,
        InterruptionTrace,
    )

ControlState = Literal["idle", "launching", "connecting", "live", "stopping"]
ActivationStatus = Literal["opened", "busy", "failed"]
StatusObserver = Callable[[ControlState], None]


class StaleControlMessage(ValueError):
    """Raised when a control message does not match the active session."""


class BrowserLauncher(Protocol):
    """Protocol for launching the browser control URL."""

    def launch(self, loopback_url: str) -> BrowserHandle: ...  # pragma: no cover - protocol shim


@dataclass
class ActivationResult:
    status: ActivationStatus
    session_id: str | None = None
    token: str | None = None
    error: str | None = None


@dataclass
class TerminalSessionResult:
    session_id: str
    outcome: SessionOutcome
    error: str | None = None


@dataclass
class _WakeSession:
    session_id: str
    token: str
    state: SessionState
    trace: SessionTrace
    interruptions: InterruptionTimelineReducer
    browser_handle: BrowserHandle | None
    teardown: TeardownCoordinator | None = None
    teardown_marked: bool = False
    stop_reason: StopReason | None = None
    resolved_result: TerminalSessionResult | None = None
    startup_deadline: BrowserStartupDeadline | None = None


class VoiceSessionController:
    """Coordinate one active local wake session and its control messages."""

    def __init__(
        self,
        launcher: BrowserLauncher,
        *,
        session_id_factory: Callable[[], str] | None = None,
        token_store: LaunchTokenStore | None = None,
        lock: asyncio.Lock | None = None,
        capability_broker: CapabilityBroker | None = None,
        browser_start_timeout_seconds: float = 20.0,
        browser_ack_timeout_seconds: float = 1.0,
        teardown_step_timeout_seconds: float = 2.0,
        farewell_timeout_seconds: float = 1.5,
        trace_directory: Path | None = None,
        status_observer: StatusObserver | None = None,
    ) -> None:
        self._launcher = launcher
        self._session_id_factory = session_id_factory or (lambda: secrets.token_urlsafe(16))
        self._token_store = token_store or LaunchTokenStore()
        self._lock = lock or asyncio.Lock()
        self._capability_broker = capability_broker or CapabilityBroker()
        self._browser_start_timeout_seconds = browser_start_timeout_seconds
        self._browser_ack_timeout_seconds = browser_ack_timeout_seconds
        self._teardown_step_timeout_seconds = teardown_step_timeout_seconds
        self._farewell_timeout_seconds = farewell_timeout_seconds
        self._trace_directory = trace_directory
        self._status_observer = status_observer
        self._last_notified_status: ControlState | None = None

        self._active_session: _WakeSession | None = None
        self._last_closed_session_id: str | None = None
        self._terminal_futures: dict[str, asyncio.Future[TerminalSessionResult]] = {}
        self._terminal_results: dict[str, TerminalSessionResult] = {}
        self._last_closed_interruption_traces: tuple[InterruptionTrace, ...] = ()
        self._outbound_messages: dict[str, asyncio.Queue[LoopbackMessage]] = {}
        self._teardown_tasks: dict[str, asyncio.Task[TeardownReport]] = {}

    @property
    def token_store(self) -> LaunchTokenStore:
        return self._token_store

    @property
    def active_session_id(self) -> str | None:
        active_session = self._active_session
        return active_session.session_id if active_session is not None else None

    @property
    def status(self) -> ControlState:
        active_session = self._active_session
        if active_session is None:
            return "idle"

        phase = active_session.state.phase
        if phase is SessionPhase.LAUNCHING:
            return "launching"
        if phase is SessionPhase.CONNECTING:
            return "connecting"
        if phase is SessionPhase.LIVE:
            return "live"
        if phase is SessionPhase.STOPPING:
            return "stopping"
        return "idle"

    async def activate(self, launch_base_url: str) -> ActivationResult:
        """Create a new local session and launch the control page."""

        async with self._lock:
            if self._active_session is not None:
                return ActivationResult(status="busy")

            session_id = self._session_id_factory()
            token = self._token_store.issue(session_id)
            state = SessionState(session_id=session_id)
            terminal_future: asyncio.Future[TerminalSessionResult] = (
                asyncio.get_running_loop().create_future()
            )
            trace = SessionTrace(session_id=session_id)
            from realtime_action_spike.openai.interruption import InterruptionTimelineReducer

            interruptions = InterruptionTimelineReducer(session_id)

            state.transition_to(SessionPhase.LAUNCHING)
            session = _WakeSession(
                session_id=session_id,
                token=token,
                state=state,
                trace=trace,
                interruptions=interruptions,
                browser_handle=None,
            )
            session.teardown = self._build_teardown_coordinator(session)

            self._active_session = session
            self._notify_status()
            self._terminal_futures[session_id] = terminal_future
            self._outbound_messages[session_id] = asyncio.Queue(maxsize=64)

            self._last_closed_session_id = None
            self._last_closed_interruption_traces = ()
            try:
                browser_handle = self._launcher.launch(
                    _append_activation_query(launch_base_url, token)
                )
            except Exception:
                session.browser_handle = None
                session.resolved_result = TerminalSessionResult(
                    session_id=session_id,
                    outcome=SessionOutcome.FAILED,
                    error="activation failed",
                )
                self._active_session = None
                self._outbound_messages.pop(session_id, None)

                _transition(session, SessionPhase.FAILED)
                self._notify_status()
                self._terminal_futures.pop(session_id, None)
                self._terminal_results[session_id] = session.resolved_result
                with contextlib.suppress(Exception):
                    if not terminal_future.done():
                        terminal_future.set_result(session.resolved_result)

                self._token_store.invalidate(token)
                return ActivationResult(
                    status="failed",
                    session_id=session_id,
                    token=token,
                    error="activation failed",
                )

            session.browser_handle = browser_handle
            session.startup_deadline = BrowserStartupDeadline(
                self._browser_start_timeout_seconds,
                lambda: self._expire_browser_startup(session),
            )
            session.startup_deadline.start()
            return ActivationResult(status="opened", session_id=session_id, token=token)

    async def _expire_browser_startup(self, session: _WakeSession) -> None:
        request = TeardownRequest(
            outcome=SessionOutcome.TIMED_OUT,
            reason=StopReason.TIMEOUT,
            error="browser startup timed out",
        )
        async with self._lock:
            if self._active_session is not session or session.state.phase not in {
                SessionPhase.LAUNCHING,
                SessionPhase.CONNECTING,
            }:
                return
            coordinator = self._begin_teardown_locked(session, request)

        await coordinator.run(request)

    async def wait_for_outbound_message(self, session_id: str) -> LoopbackMessage:
        """Wait for the next sanitized controller-to-page message."""

        async with self._lock:
            self._require_active_session(session_id)
            queue = self._outbound_messages[session_id]
        return await queue.get()

    async def wait_for_terminal_result(self, session_id: str) -> TerminalSessionResult:
        """Wait for the terminal outcome tied to an exact local session id."""

        async with self._lock:
            if session_id in self._terminal_results:
                return self._terminal_results[session_id]

            future = self._terminal_futures.get(session_id)
            if future is None:
                raise ValueError("unknown session id")

        return await asyncio.shield(future)

    def validate_activation_token(self, token: str) -> str | None:
        return self._token_store.validate(token)

    async def consume_activation_token(self, token: str) -> str | None:
        async with self._lock:
            local_session_id = self._token_store.consume(token)
            if local_session_id is None:
                return None

            active_session = self._active_session
            if active_session is None or active_session.session_id != local_session_id:
                return None

            return local_session_id

    async def begin_realtime_response(
        self,
        session_id: str,
        response_id: str,
        *,
        received_ns: int,
        provider_audio_start_ms: int | None,
    ) -> None:
        """Bind first audio for one explicit provider response to the active session."""

        async with self._lock:
            active = self._require_active_session(session_id)
            active.interruptions.begin_response(
                response_id,
                received_ns=received_ns,
                provider_audio_start_ms=provider_audio_start_ms,
            )
            data: dict[str, JsonValue] = {"response_id": response_id}
            if provider_audio_start_ms is not None:
                data["provider_audio_start_ms"] = provider_audio_start_ms
            active.trace.record(
                "response_first_audio",
                source="openai",
                data=data,
                monotonic_ns=received_ns,
            )

    async def record_interruption_event(
        self,
        session_id: str,
        event: InterruptionEvent,
    ) -> bool:
        """Record one exact-session observation, rejecting stale response events."""

        async with self._lock:
            active = self._require_active_session(session_id)
            if event.local_session_id != session_id:
                raise StaleControlMessage("interruption event session mismatch")
            accepted = active.interruptions.consume(event)
            if not accepted:
                return False
            source = (
                "browser"
                if event.kind.value in {"playback_suppressed", "listening_restored"}
                else "openai"
            )
            data: dict[str, JsonValue] = {"response_id": event.response_id}
            if event.user_speech_onset_ms is not None:
                data["user_speech_onset_ms"] = event.user_speech_onset_ms
            active.trace.record(
                event.kind.value,
                source=source,
                data=data,
                monotonic_ns=event.occurred_ns,
            )
            return True

    def _build_teardown_coordinator(self, session: _WakeSession) -> TeardownCoordinator:
        async def mark_stopping(request: TeardownRequest) -> None:
            async with self._lock:
                self._begin_teardown_locked(session, request)

        async def request_browser_stop(request: TeardownRequest) -> None:
            async with self._lock:
                active = self._require_active_session(session.session_id)
                queue = self._outbound_messages[session.session_id]
                message = StopMessage(
                    type="stop",
                    session_id=session.session_id,
                    reason=request.reason,
                )
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(message)
                active.trace.record(
                    "browser_stop_requested",
                    source="controller",
                    data={"reason": message.reason.value},
                )


        async def close_browser() -> None:
            async with self._lock:
                browser_handle = session.browser_handle
                session.browser_handle = None
            if browser_handle is not None:
                await asyncio.to_thread(browser_handle.close)

        async def release_profile_lock() -> None:
            # Runs after close_browser, so the owned process group has exited and
            # the dedicated profile's singleton lock can be released safely. This
            # keeps the profile from going stale even on hard-kill / timeout paths.
            release = getattr(self._launcher, "release_profile_lock", None)
            if release is None:
                return
            await asyncio.to_thread(release)

        async def persist_trace(failures: tuple[TeardownStepFailure, ...]) -> None:
            for failure in failures:
                session.trace.record(
                    "teardown_step_failed",
                    source="controller",
                    data={"step": failure.step, "kind": failure.kind},
                )
            session.trace.record("teardown_complete", source="controller")
            if self._trace_directory is None:
                return

            trace_path = self._trace_directory / f"{session.session_id}.jsonl"

            def write_trace() -> None:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                session.trace.write_jsonl(trace_path)

            await asyncio.to_thread(write_trace)

        async def finalize(
            request: TeardownRequest,
            _failures: tuple[TeardownStepFailure, ...],
        ) -> None:
            async with self._lock:
                active = self._active_session
                if active is not session:
                    return

                terminal_phase = (
                    SessionPhase.FAILED
                    if request.outcome is SessionOutcome.FAILED
                    else SessionPhase.CLOSED
                )
                _transition(session, terminal_phase)
                self._last_closed_interruption_traces = copy.deepcopy(
                    session.interruptions.traces
                )
                self._active_session = None
                self._notify_status()
                self._last_closed_session_id = session.session_id
                self._outbound_messages.pop(session.session_id, None)

                if session.startup_deadline is not None:
                    session.startup_deadline.cancel()
                    session.startup_deadline = None
                self._teardown_tasks.pop(session.session_id, None)
                self._resolve_terminal_result(
                    session,
                    outcome=request.outcome,
                    error=request.error,
                )


        return TeardownCoordinator(
            TeardownHooks(
                mark_stopping=mark_stopping,
                request_browser_stop=request_browser_stop,

                close_browser=close_browser,
                persist_trace=persist_trace,
                finalize=finalize,
                release_profile_lock=release_profile_lock,
            ),
            acknowledgement_timeout=self._browser_ack_timeout_seconds,
            step_timeout=self._teardown_step_timeout_seconds,
        )

    def _begin_teardown_locked(
        self,
        session: _WakeSession,
        request: TeardownRequest,
    ) -> TeardownCoordinator:
        active = self._require_active_session(session.session_id)
        if active.state.phase is not SessionPhase.STOPPING:
            _transition(active, SessionPhase.STOPPING)
            self._notify_status()
        if active.stop_reason is None:
            active.stop_reason = request.reason
        if not active.teardown_marked:
            active.trace.record(
                "teardown_requested",
                source="controller",
                data={
                    "outcome": request.outcome.value,
                    "reason": request.reason.value,
                },
            )
            active.teardown_marked = True
        if active.teardown is None:  # pragma: no cover - construction invariant
            raise RuntimeError("teardown coordinator is not configured")
        active.teardown.start(request)
        return active.teardown

    async def request_teardown(
        self,
        session_id: str,
        *,
        outcome: SessionOutcome,
        reason: StopReason,
        error: str | None = None,
        shield: bool = True,
    ) -> TeardownReport:
        """Run the exact-session teardown once; the first terminal request wins."""

        request = TeardownRequest(outcome=outcome, error=error, reason=reason)
        async with self._lock:
            session = self._require_active_session(session_id)
            coordinator = self._begin_teardown_locked(session, request)
        return await coordinator.run(request, shield=shield)

    async def interruption_traces(self, session_id: str) -> tuple[InterruptionTrace, ...]:
        """Return an isolated snapshot for the active or most recently closed session."""

        async with self._lock:
            active = self._active_session
            if active is not None and active.session_id == session_id:
                return copy.deepcopy(active.interruptions.traces)
            if session_id == self._last_closed_session_id:
                return copy.deepcopy(self._last_closed_interruption_traces)
            raise StaleControlMessage("interruption traces are not available for session")

    async def process_control_message(
        self,
        session_id: str,
        raw_message: str,
    ) -> SessionClosedMessage | None:
        message = parse_loopback_message(raw_message, expected_session_id=session_id)
        coordinator: TeardownCoordinator | None = None
        request: TeardownRequest | None = None

        async with self._lock:
            active = self._active_session

            if active is None:
                if session_id == self._last_closed_session_id and isinstance(
                    message,
                    TeardownCompleteMessage,
                ):
                    resolved = self._terminal_results.get(session_id)
                    outcome = (
                        resolved.outcome if resolved is not None else SessionOutcome.COMPLETED
                    )
                    return SessionClosedMessage(
                        type="session_closed",
                        session_id=session_id,
                        outcome=outcome,
                    )
                raise StaleControlMessage("no active session")

            if active.session_id != session_id:
                raise StaleControlMessage("stale session id")

            if isinstance(message, PageReadyMessage):
                _transition(active, SessionPhase.CONNECTING)
                self._notify_status()
                return None

            if isinstance(message, PageStartedMessage):
                _transition(active, SessionPhase.LIVE)
                if active.startup_deadline is not None:
                    active.startup_deadline.cancel()
                    active.startup_deadline = None
                self._notify_status()
                return None


            if isinstance(message, TimingMessage):
                diagnostic_data = sanitize_timing_data(message.data)
                active.trace.record(
                    message.name.value,
                    source="browser",
                    data=diagnostic_data,
                    monotonic_ns=int(message.monotonic_ms * 1_000_000),
                )
                logger.info(
                    "session_diagnostic %s",
                    json.dumps(
                        {
                            "session_id": session_id,
                            "name": message.name.value,
                            "data": diagnostic_data,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                return None

            if isinstance(message, StopMessage):
                outcome = self._outcome_for_stop_reason(message.reason)
                request = TeardownRequest(outcome=outcome, reason=message.reason)
                coordinator = self._begin_teardown_locked(active, request)
                self._teardown_tasks[session_id] = coordinator.start(request)
                return None

            if isinstance(message, TeardownCompleteMessage):
                reason = active.stop_reason or StopReason.BUTTON
                request = TeardownRequest(
                    outcome=self._outcome_for_stop_reason(reason),
                    reason=reason,
                )
                coordinator = self._begin_teardown_locked(active, request)
                coordinator.acknowledge_browser_teardown()

        if coordinator is None or request is None:  # pragma: no cover - protocol exhaustiveness
            raise RuntimeError("unreachable")

        report = await coordinator.run(request)
        return SessionClosedMessage(
            type="session_closed",
            session_id=session_id,
            outcome=report.request.outcome,
        )

    async def close_active_session(
        self,
        *,
        outcome: SessionOutcome = SessionOutcome.CANCELLED,
        error: str | None = None,
    ) -> None:
        """Converge service or tray shutdown on the shared teardown path."""

        async with self._lock:
            active = self._active_session
            if active is None:
                return
            session_id = active.session_id

        reason = (
            StopReason.TRANSPORT_FAILURE
            if outcome is SessionOutcome.FAILED
            else StopReason.NATIVE_CANCEL
        )
        await self.request_teardown(
            session_id,
            outcome=outcome,
            reason=reason,
            error=error,
            shield=False,
        )

    @staticmethod
    def _outcome_for_stop_reason(reason: StopReason) -> SessionOutcome:
        if reason is StopReason.TRANSPORT_FAILURE:
            return SessionOutcome.FAILED
        if reason is StopReason.TIMEOUT:
            return SessionOutcome.TIMED_OUT
        if reason is StopReason.NATIVE_CANCEL:
            return SessionOutcome.CANCELLED
        return SessionOutcome.COMPLETED

    def _resolve_terminal_result(
        self,
        session: _WakeSession,
        *,
        outcome: SessionOutcome,
        error: str | None = None,
    ) -> TerminalSessionResult:
        if session.resolved_result is not None:
            return session.resolved_result

        result = TerminalSessionResult(session_id=session.session_id, outcome=outcome, error=error)
        session.resolved_result = result
        self._terminal_results[session.session_id] = result

        future = self._terminal_futures.pop(session.session_id, None)
        if future is not None and not future.done():
            future.set_result(result)

        self._token_store.invalidate(session.token)

        return result

    def _require_active_session(self, session_id: str) -> _WakeSession:
        active = self._active_session
        if active is None or active.session_id != session_id:
            raise StaleControlMessage("stale session id")
        return active

    def _notify_status(self) -> None:
        status = self.status
        if status == self._last_notified_status:
            return
        self._last_notified_status = status
        if self._status_observer is not None:
            self._status_observer(status)


def _append_activation_query(url: str, token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["activation"] = [token]
    encoded_query = urlencode(query, doseq=True)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, encoded_query, parsed.fragment),
    )


def _transition(session: _WakeSession, phase: SessionPhase) -> None:
    try:
        session.state.transition_to(phase)
    except SessionTransitionError:
        if session.state.phase == phase:
            return
        raise
