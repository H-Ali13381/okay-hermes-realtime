from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .browser import BrowserHandle
from .protocol import (
    PageReadyMessage,
    PageStartedMessage,
    SessionClosedMessage,
    SessionOutcome,
    StopMessage,
    TeardownCompleteMessage,
    TimingMessage,
    parse_loopback_message,
)
from .session_state import SessionPhase, SessionState, SessionTransitionError
from .timing import SessionTrace, sanitize_timing_data
from .tokens import LaunchTokenStore

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


@dataclass
class _WakeSession:
    session_id: str
    token: str
    state: SessionState
    trace: SessionTrace
    browser_handle: BrowserHandle


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
            state.transition_to(SessionPhase.LAUNCHING)
            trace = SessionTrace(session_id=session_id)

            self._last_closed_session_id = None
            try:
                browser_handle = self._launcher.launch(
                    _append_activation_query(launch_base_url, token)
                )
            except Exception:
                self._token_store.invalidate(token)
                state.transition_to(SessionPhase.FAILED)
                return ActivationResult(status="failed", session_id=session_id, token=token)

            self._active_session = _WakeSession(
                session_id=session_id,
                token=token,
                state=state,
                trace=trace,
                browser_handle=browser_handle,
            )
            return ActivationResult(status="opened", session_id=session_id, token=token)

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
                    message, TeardownCompleteMessage
                ):
                    return SessionClosedMessage(
                        type="session_closed",
                        session_id=session_id,
                        outcome=SessionOutcome.COMPLETED,
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
                _transition(active, SessionPhase.STOPPING)
                return None

            if isinstance(message, TeardownCompleteMessage):
                if active.state.phase != SessionPhase.STOPPING:
                    _transition(active, SessionPhase.STOPPING)
                _transition(active, SessionPhase.CLOSED)

                self._active_session = None
                self._last_closed_session_id = session_id

                with contextlib.suppress(Exception):
                    active.browser_handle.close()
                return SessionClosedMessage(
                    type="session_closed",
                    session_id=session_id,
                    outcome=SessionOutcome.COMPLETED,
                )

        raise RuntimeError("unreachable")


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
