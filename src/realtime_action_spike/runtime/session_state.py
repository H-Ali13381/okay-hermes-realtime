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
    history: list[SessionPhase] = field(default_factory=list)

    _TRANSITIONS: ClassVar[dict[SessionPhase, set[SessionPhase]]] = {
        SessionPhase.IDLE: {SessionPhase.LAUNCHING},
        SessionPhase.LAUNCHING: {SessionPhase.CONNECTING, SessionPhase.FAILED},
        SessionPhase.CONNECTING: {SessionPhase.LIVE, SessionPhase.FAILED},
        SessionPhase.LIVE: {SessionPhase.STOPPING, SessionPhase.FAILED},
        SessionPhase.STOPPING: {SessionPhase.STOPPING, SessionPhase.CLOSED, SessionPhase.FAILED},
        SessionPhase.CLOSED: set(),
        SessionPhase.FAILED: set(),
    }

    def __post_init__(self) -> None:
        if not self.history:
            self.history = [self.phase]

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
