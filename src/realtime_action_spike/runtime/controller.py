from __future__ import annotations

import asyncio
import contextlib
import copy
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from realtime_action_spike.openai.sideband import RealtimeSidebandClient, SidebandEvent

from .browser import BrowserHandle
from .protocol import (
    ActionStateMessage,
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
from .session_state import SessionPhase, SessionState, SessionTransitionError
from .timing import JsonValue, SessionTrace, sanitize_timing_data
from .tokens import LaunchTokenStore

if TYPE_CHECKING:
    from realtime_action_spike.openai.interruption import (
        InterruptionEvent,
        InterruptionTimelineReducer,
        InterruptionTrace,
    )

ControlState = Literal["idle", "launching", "connecting", "live", "stopping"]
ActivationStatus = Literal["opened", "busy", "failed"]


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
    stop_reason: StopReason | None = None
    resolved_result: TerminalSessionResult | None = None


class VoiceSessionController:
    """Coordinate one active local wake session and its control messages."""

    def __init__(
        self,
        launcher: BrowserLauncher,
        *,
        session_id_factory: Callable[[], str] | None = None,
        token_store: LaunchTokenStore | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._launcher = launcher
        self._session_id_factory = session_id_factory or (lambda: secrets.token_urlsafe(16))
        self._token_store = token_store or LaunchTokenStore()
        self._lock = lock or asyncio.Lock()

        self._active_session: _WakeSession | None = None
        self._last_closed_session_id: str | None = None
        self._terminal_futures: dict[str, asyncio.Future[TerminalSessionResult]] = {}
        self._terminal_results: dict[str, TerminalSessionResult] = {}
        self._last_closed_interruption_traces: tuple[InterruptionTrace, ...] = ()
        self._sideband_clients: dict[str, RealtimeSidebandClient] = {}
        self._outbound_messages: dict[str, asyncio.Queue[ActionStateMessage]] = {}

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

            self._active_session = session
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
            return ActivationResult(status="opened", session_id=session_id, token=token)

    async def publish_action_state(self, message: ActionStateMessage) -> None:
        """Queue a sanitized action-state message for the exact active page."""

        async with self._lock:
            self._require_active_session(message.session_id)
            queue = self._outbound_messages[message.session_id]
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    async def wait_for_outbound_message(self, session_id: str) -> ActionStateMessage:
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

    async def start_realtime_sideband(
        self,
        *,
        local_session_id: str,
        call_id: str,
        api_key: str,
        websocket_connect: Callable[[str, dict[str, str]], Any] | None = None,
    ) -> None:
        """Start a server-side sideband connection for the exact local session."""

        async def on_event(event: SidebandEvent) -> None:
            try:
                await self.process_sideband_event(event.local_session_id, event.payload)
            except StaleControlMessage:
                await self._detach_sideband(event.local_session_id)
            except Exception as error:  # pragma: no cover - defensive path
                await self._handle_sideband_failure(event.local_session_id, error)

        async def on_terminal_failure(local_session_id: str, error: Exception) -> None:
            await self._handle_sideband_failure(local_session_id, error)

        sideband = RealtimeSidebandClient(
            local_session_id=local_session_id,
            call_id=call_id,
            api_key=api_key,
            on_event=on_event,
            on_terminal_failure=on_terminal_failure,
            websocket_connect=websocket_connect,
        )

        async with self._lock:
            self._require_active_session(local_session_id)
            previous_sideband = self._sideband_clients.pop(local_session_id, None)
            self._sideband_clients[local_session_id] = sideband

        if previous_sideband is not None:
            await previous_sideband.close()

        try:
            await sideband.connect()
        except Exception:
            async with self._lock:
                current = self._sideband_clients.get(local_session_id)
                if current is sideband:
                    self._sideband_clients.pop(local_session_id, None)
            await sideband.close()
            raise

    async def process_sideband_event(
        self,
        local_session_id: str,
        payload: Any,
    ) -> None:
        """Apply one sideband event for an exact local session."""

        async with self._lock:
            self._require_active_session(local_session_id)
            if not isinstance(payload, dict):
                raise TypeError("sideband event payload must be an object")

    async def send_sideband_event(
        self,
        local_session_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Send one event through the authoritative sideband for the exact session."""

        async with self._lock:
            self._require_active_session(local_session_id)
            sideband = self._sideband_clients.get(local_session_id)
            if sideband is None:
                raise RuntimeError("sideband is not connected")

        await sideband.send_json(payload)

    async def _handle_sideband_failure(self, local_session_id: str, _error: Exception) -> None:
        await self._detach_sideband(local_session_id)

        active = self._active_session
        if active is None or active.session_id != local_session_id:
            return

        await self.close_active_session(
            outcome=SessionOutcome.FAILED,
            error="sideband connection failed",
        )

    async def _detach_sideband(self, local_session_id: str) -> None:
        sideband = self._sideband_clients.pop(local_session_id, None)
        if sideband is None:
            return
        await sideband.close()

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
                return None

            if isinstance(message, PageStartedMessage):
                _transition(active, SessionPhase.LIVE)
                return None

            if isinstance(message, TimingMessage):
                active.trace.record(
                    message.name.value,
                    source="browser",
                    data=sanitize_timing_data(message.data),
                    monotonic_ns=int(message.monotonic_ms * 1_000_000),
                )
                return None

            if isinstance(message, StopMessage):
                active.stop_reason = message.reason
                _transition(active, SessionPhase.STOPPING)
                return None

            if isinstance(message, TeardownCompleteMessage):
                if active.state.phase != SessionPhase.STOPPING:
                    _transition(active, SessionPhase.STOPPING)
                return await self._close_active_session_locked(
                    active,
                )

        raise RuntimeError("unreachable")

    async def close_active_session(
        self,
        *,
        outcome: SessionOutcome = SessionOutcome.CANCELLED,
        error: str | None = None,
    ) -> None:
        """Stop and mark the current active session during service shutdown."""

        sideband: RealtimeSidebandClient | None = None
        async with self._lock:
            active = self._active_session
            if active is None:
                return
            sideband = self._sideband_clients.pop(active.session_id, None)
            self._last_closed_interruption_traces = copy.deepcopy(
                active.interruptions.traces
            )
            self._active_session = None
            self._last_closed_session_id = active.session_id
            self._outbound_messages.pop(active.session_id, None)

            browser_handle = active.browser_handle
            if browser_handle is not None:
                with contextlib.suppress(Exception):
                    browser_handle.close()

            resolved = self._resolve_terminal_result(
                active,
                outcome=outcome,
                error=error,
            )
            # Keep terminal outcome accessible after cleanup for shutdown completion.
            self._terminal_results[active.session_id] = resolved

        if sideband is not None:
            await sideband.close()

    async def _close_active_session_locked(
        self,
        session: _WakeSession,
    ) -> SessionClosedMessage:
        sideband = self._sideband_clients.pop(session.session_id, None)
        _transition(session, SessionPhase.CLOSED)
        outcome = self._resolve_session_outcome(session)
        resolved = self._resolve_terminal_result(
            session,
            outcome=outcome,
            error=None,
        )

        self._last_closed_interruption_traces = copy.deepcopy(
            session.interruptions.traces
        )
        self._active_session = None
        self._last_closed_session_id = session.session_id
        self._outbound_messages.pop(session.session_id, None)

        if session.browser_handle is not None:
            with contextlib.suppress(Exception):
                session.browser_handle.close()
        if sideband is not None:
            await sideband.close()

        return SessionClosedMessage(
            type="session_closed",
            session_id=session.session_id,
            outcome=resolved.outcome,
        )

    def _resolve_session_outcome(self, session: _WakeSession) -> SessionOutcome:
        if session.stop_reason is StopReason.TRANSPORT_FAILURE:
            return SessionOutcome.FAILED
        if session.stop_reason is StopReason.TIMEOUT:
            return SessionOutcome.TIMED_OUT
        if session.stop_reason is StopReason.NATIVE_CANCEL:
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
