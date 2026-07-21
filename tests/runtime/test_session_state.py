from __future__ import annotations

import pytest

from realtime_action_spike.runtime.session_state import (
    SessionPhase,
    SessionState,
    SessionTransitionError,
)


def test_session_phase_requires_ordered_transitions() -> None:
    state = SessionState(session_id="session-order")

    assert state.phase == SessionPhase.IDLE

    for next_phase in (
        SessionPhase.LAUNCHING,
        SessionPhase.CONNECTING,
        SessionPhase.LIVE,
        SessionPhase.STOPPING,
        SessionPhase.CLOSED,
    ):
        state.transition_to(next_phase)

    assert state.phase == SessionPhase.CLOSED
    assert state.history == [
        SessionPhase.IDLE,
        SessionPhase.LAUNCHING,
        SessionPhase.CONNECTING,
        SessionPhase.LIVE,
        SessionPhase.STOPPING,
        SessionPhase.CLOSED,
    ]


def test_closed_and_failed_states_cannot_resurrect() -> None:
    def _to_failed() -> SessionState:
        state = SessionState(session_id="session-resurrection")
        state.transition_to(SessionPhase.LAUNCHING)
        state.transition_to(SessionPhase.FAILED)
        return state

    closed_state = SessionState(session_id="session-resurrection")
    closed_state.transition_to(SessionPhase.LAUNCHING)
    closed_state.transition_to(SessionPhase.CONNECTING)
    closed_state.transition_to(SessionPhase.LIVE)
    closed_state.transition_to(SessionPhase.STOPPING)
    closed_state.transition_to(SessionPhase.CLOSED)

    failed_state = _to_failed()

    for next_phase in SessionPhase:
        if next_phase in {SessionPhase.CLOSED, SessionPhase.FAILED}:
            continue
        with pytest.raises(
            SessionTransitionError,
            match=r"cannot transition",
        ):
            closed_state.transition_to(next_phase)
        with pytest.raises(
            SessionTransitionError,
            match=r"cannot transition",
        ):
            failed_state.transition_to(next_phase)


def test_repeated_stop_is_idempotent() -> None:
    state = SessionState(session_id="session-stop")
    state.transition_to(SessionPhase.LAUNCHING)
    state.transition_to(SessionPhase.CONNECTING)
    state.transition_to(SessionPhase.LIVE)

    state.transition_to(SessionPhase.STOPPING)
    state.transition_to(SessionPhase.STOPPING)

    assert state.phase == SessionPhase.STOPPING
    assert state.history.count(SessionPhase.STOPPING) == 1
