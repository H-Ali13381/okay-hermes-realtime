from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar


class SessionPhase(StrEnum):
    """Voice session lifecycle phases.

    The replacement follows OpenAI WebRTC control flow directly:
    idle -> launching -> connecting -> live -> stopping -> closed.
    """

    IDLE = "idle"
    LAUNCHING = "launching"
    CONNECTING = "connecting"
    LIVE = "live"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


class SessionTransitionError(ValueError):
    """Raised when a lifecycle transition is not legal for the current state."""


@dataclass(slots=True)
class SessionState:
    """Tracks explicit session phase transitions with an auditable history."""

    session_id: str
    phase: SessionPhase = SessionPhase.IDLE
    history: list[SessionPhase] = field(default_factory=lambda: [SessionPhase.IDLE])

    _TRANSITIONS: ClassVar[dict[SessionPhase, set[SessionPhase]]] = {
        SessionPhase.IDLE: {SessionPhase.LAUNCHING},
        SessionPhase.LAUNCHING: {
            SessionPhase.CONNECTING,
            SessionPhase.STOPPING,
            SessionPhase.FAILED,
        },
        SessionPhase.CONNECTING: {
            SessionPhase.LIVE,
            SessionPhase.STOPPING,
            SessionPhase.FAILED,
        },
        SessionPhase.LIVE: {SessionPhase.STOPPING, SessionPhase.FAILED},
        SessionPhase.STOPPING: {SessionPhase.STOPPING, SessionPhase.CLOSED, SessionPhase.FAILED},
        SessionPhase.CLOSED: set(),
        SessionPhase.FAILED: set(),
    }

    def __post_init__(self) -> None:
        if not self.history:
            raise ValueError("history must not be empty")

        if self.history[0] is not SessionPhase.IDLE:
            raise ValueError("history must begin with SessionPhase.IDLE")

        for previous_phase, next_phase in zip(self.history, self.history[1:], strict=False):
            if next_phase not in self._TRANSITIONS[previous_phase]:
                raise SessionTransitionError(
                    f"cannot transition from {previous_phase} to {next_phase}"
                )

        if self.history[-1] is not self.phase:
            raise ValueError("history must end at the current phase")

    def transition_to(self, next_phase: SessionPhase) -> None:
        """Advance to ``next_phase`` if it is explicitly legal from the current phase."""

        allowed = self._TRANSITIONS[self.phase]
        if next_phase not in allowed:
            raise SessionTransitionError(
                f"cannot transition from {self.phase} to {next_phase}"
            )

        if next_phase == self.phase == SessionPhase.STOPPING:
            return

        self.phase = next_phase
        self.history.append(next_phase)
