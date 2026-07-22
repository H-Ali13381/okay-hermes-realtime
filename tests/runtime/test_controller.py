from __future__ import annotations

import asyncio

import pytest

from realtime_action_spike.runtime.controller import (
    StaleControlMessage,
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


class DeterministicLauncher:
    def __init__(self) -> None:
        self.launched_urls: list[str] = []
        self.calls = 0

    def launch(self, loopback_url: str) -> None:
        self.calls += 1
        self.launched_urls.append(loopback_url)


class FailingLauncher:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def launch(self, loopback_url: str) -> None:
        self.calls += 1
        raise self.error


class SequenceFactory:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        value = self._values[self._index]
        self._index += 1
        return value


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
    assert statuses[0].session_id is not None or statuses[1].session_id is not None


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
    assert launcher.calls == 1


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
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory(["token-1"]),
        ),
        session_id_factory=SequenceFactory(["local-session-01"]),
    )

    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id
    assert session_id is not None

    assert controller.status == "launching"

    ready = encode_loopback_message(
        PageReadyMessage(type="page_ready", session_id=session_id)
    )
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
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_stale_session_messages_are_rejected_after_newer_session_starts() -> None:
    controller = VoiceSessionController(
        DeterministicLauncher(),
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
