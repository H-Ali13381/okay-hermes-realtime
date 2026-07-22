from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from realtime_action_spike.capabilities import CapabilityBroker
from realtime_action_spike.runtime.controller import StaleControlMessage, VoiceSessionController
from realtime_action_spike.runtime.protocol import (
    ActionStateMessage,
    PageReadyMessage,
    PageStartedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    TimingMessage,
    TimingName,
    encode_loopback_message,
)
from realtime_action_spike.runtime.tokens import LaunchTokenStore
from tests.fakes.fake_activation_client import ActivationTranscript, FakeActivationClient
from tests.fakes.fake_browser import FakeBrowserLauncher
from tests.fakes.fake_sideband import FakeSidebandConnector, FakeSidebandWebSocket


class SequenceFactory:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class EndSessionBroker(CapabilityBroker):
    def execute(
        self,
        name: str,
        arguments: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        assert name == "voice_end_session"
        assert not isinstance(arguments, str)
        return {
            "ok": True,
            "capability": name,
            "execution": "local",
            "result": {"end_session": True},
        }


def make_controller(
    launcher: FakeBrowserLauncher,
    *session_ids: str,
    broker: CapabilityBroker | None = None,
) -> VoiceSessionController:
    tokens = tuple(f"token-{index}" for index in range(len(session_ids)))
    return VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(token_factory=SequenceFactory(*tokens)),
        session_id_factory=SequenceFactory(*session_ids),
        capability_broker=broker,
        browser_ack_timeout_seconds=0.01,
        farewell_timeout_seconds=0.01,
    )


async def open_client(
    controller: VoiceSessionController,
) -> tuple[FakeActivationClient, asyncio.Task[ActivationTranscript], str, str]:
    client = FakeActivationClient(controller, "http://127.0.0.1:8765/voice")
    task = client.start()
    await asyncio.wait_for(client.activation_started.wait(), timeout=0.2)
    activation = client.activation
    assert activation is not None
    assert activation.session_id is not None
    assert activation.token is not None
    return client, task, activation.session_id, activation.token


async def mark_live(controller: VoiceSessionController, session_id: str) -> None:
    await controller.process_control_message(
        session_id,
        encode_loopback_message(PageReadyMessage(type="page_ready", session_id=session_id)),
    )
    await controller.process_control_message(
        session_id,
        encode_loopback_message(PageStartedMessage(type="page_started", session_id=session_id)),
    )


@pytest.mark.asyncio
async def test_browser_crash_releases_activation_and_returns_idle() -> None:
    launcher = FakeBrowserLauncher()
    controller = make_controller(launcher, "local-browser-crash-01")
    _, activation_task, session_id, _ = await open_client(controller)

    report = await controller.request_teardown(
        session_id,
        outcome=SessionOutcome.FAILED,
        reason=StopReason.TRANSPORT_FAILURE,
        error="control websocket disconnected",
    )
    transcript = await asyncio.wait_for(activation_task, timeout=0.2)

    assert not report.browser_acknowledged
    assert transcript.terminal is not None
    assert transcript.terminal.outcome is SessionOutcome.FAILED
    assert controller.status == "idle"
    assert launcher.handles[0].close_calls == 1


@pytest.mark.asyncio
async def test_sideband_loss_releases_activation_and_returns_idle() -> None:
    launcher = FakeBrowserLauncher()
    controller = make_controller(launcher, "local-sideband-loss-01")
    _, activation_task, session_id, _ = await open_client(controller)
    await mark_live(controller, session_id)
    websocket = FakeSidebandWebSocket()
    await controller.start_realtime_sideband(
        local_session_id=session_id,
        call_id="call-sideband-loss",
        api_key="server-secret",
        websocket_connect=FakeSidebandConnector(websocket),
    )

    websocket.push(RuntimeError("injected provider disconnect secret-value"))
    transcript = await asyncio.wait_for(activation_task, timeout=0.3)

    assert transcript.terminal is not None
    assert transcript.terminal.outcome is SessionOutcome.FAILED
    assert transcript.terminal.error == "sideband connection failed"
    assert controller.status == "idle"
    assert websocket.closed


@pytest.mark.asyncio
async def test_setup_timeout_uses_shared_teardown_and_returns_idle() -> None:
    launcher = FakeBrowserLauncher()
    controller = make_controller(launcher, "local-setup-timeout-01")
    _, activation_task, session_id, _ = await open_client(controller)

    teardown = asyncio.create_task(
        controller.request_teardown(
            session_id,
            outcome=SessionOutcome.TIMED_OUT,
            reason=StopReason.TIMEOUT,
        )
    )
    assert await controller.wait_for_outbound_message(session_id) == StopMessage(
        type="stop",
        session_id=session_id,
        reason=StopReason.TIMEOUT,
    )
    await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )
    await teardown
    transcript = await asyncio.wait_for(activation_task, timeout=0.2)

    assert transcript.terminal is not None
    assert transcript.terminal.outcome is SessionOutcome.TIMED_OUT
    assert controller.status == "idle"


@pytest.mark.asyncio
async def test_close_phrase_farewell_timeout_converges_on_shared_teardown() -> None:
    launcher = FakeBrowserLauncher()
    controller = make_controller(
        launcher,
        "local-close-phrase-01",
        broker=EndSessionBroker(),
    )
    _, activation_task, session_id, _ = await open_client(controller)
    websocket = FakeSidebandWebSocket()
    await controller.start_realtime_sideband(
        local_session_id=session_id,
        call_id="call-close-phrase",
        api_key="server-secret",
        websocket_connect=FakeSidebandConnector(websocket),
    )

    await controller.process_sideband_event(
        session_id,
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call-end-session",
            "name": "voice_end_session",
            "arguments": "{}",
        },
    )
    transcript = await asyncio.wait_for(activation_task, timeout=0.3)

    assert transcript.terminal is not None
    assert transcript.terminal.outcome is SessionOutcome.COMPLETED
    assert controller.status == "idle"
    assert websocket.closed


@pytest.mark.asyncio
async def test_busy_replay_stale_events_and_repeated_cleanup_are_isolated() -> None:
    launcher = FakeBrowserLauncher()
    controller = make_controller(
        launcher,
        "local-isolation-01",
        "local-isolation-02",
    )
    _, first_task, first_id, first_token = await open_client(controller)
    assert await controller.consume_activation_token(first_token) == first_id
    assert await controller.consume_activation_token(first_token) is None
    await mark_live(controller, first_id)

    busy = await controller.activate("http://127.0.0.1:8765/voice")
    assert busy.status == "busy"
    assert len(launcher.launched_urls) == 1

    await controller.process_control_message(
        first_id,
        encode_loopback_message(
            StopMessage(type="stop", session_id=first_id, reason=StopReason.BUTTON)
        ),
    )
    await controller.wait_for_outbound_message(first_id)
    teardown_message = encode_loopback_message(
        TeardownCompleteMessage(type="teardown_complete", session_id=first_id)
    )
    first_closed = await controller.process_control_message(first_id, teardown_message)
    duplicate_closed = await controller.process_control_message(first_id, teardown_message)
    assert duplicate_closed == first_closed
    await asyncio.wait_for(first_task, timeout=0.2)

    second = await controller.activate("http://127.0.0.1:8765/voice")
    assert second.status == "opened"
    assert second.session_id == "local-isolation-02"

    stale_messages = (
        PageStartedMessage(type="page_started", session_id=first_id),
        TimingMessage(
            type="timing",
            session_id=first_id,
            name=TimingName.PLAYBACK_SUPPRESSED,
            monotonic_ms=1.0,
            data={"response_id": "resp-stale"},
        ),
    )
    for message in stale_messages:
        with pytest.raises(StaleControlMessage):
            await controller.process_control_message(
                first_id,
                encode_loopback_message(message),
            )
    with pytest.raises(StaleControlMessage):
        await controller.process_sideband_event(
            first_id,
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-stale",
                "name": "assistant_get_current_time",
                "arguments": "{}",
            },
        )
    with pytest.raises(StaleControlMessage):
        await controller.publish_action_state(
            ActionStateMessage(
                type="action_state",
                session_id=first_id,
                capability="assistant_get_current_time",
                state="running",
                message="stale",
            )
        )

    assert controller.active_session_id == "local-isolation-02"
    await controller.close_active_session()
