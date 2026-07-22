from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from realtime_action_spike.capabilities import CapabilityBroker
from realtime_action_spike.openai.interruption import (
    InterruptionEvent,
    InterruptionEventKind,
)
from realtime_action_spike.runtime.controller import VoiceSessionController
from realtime_action_spike.runtime.protocol import (
    ActionStateMessage,
    PageReadyMessage,
    PageStartedMessage,
    SessionOutcome,
    StopMessage,
    StopReason,
    TeardownCompleteMessage,
    encode_loopback_message,
)
from realtime_action_spike.runtime.tokens import LaunchTokenStore
from tests.fakes.fake_activation_client import FakeActivationClient
from tests.fakes.fake_browser import FakeBrowserLauncher
from tests.fakes.fake_sideband import FakeSidebandConnector, FakeSidebandWebSocket


class SequenceFactory:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


@dataclass
class FixedClockBroker(CapabilityBroker):
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def execute(
        self,
        name: str,
        arguments: str | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(arguments, str):
            raise AssertionError("trusted tool loop must pass parsed arguments")
        self.calls.append((name, dict(arguments)))
        return {
            "ok": True,
            "capability": name,
            "execution": "local",
            "result": {"timezone": "UTC", "iso": "2026-07-22T03:00:00+00:00"},
        }


async def send_control(
    controller: VoiceSessionController,
    session_id: str,
    message: PageReadyMessage
    | PageStartedMessage
    | StopMessage
    | TeardownCompleteMessage,
) -> object:
    return await controller.process_control_message(
        session_id,
        encode_loopback_message(message),
    )


@pytest.mark.asyncio
async def test_wake_page_sideband_interruption_teardown_and_rearm() -> None:
    launcher = FakeBrowserLauncher()
    broker = FixedClockBroker()
    controller = VoiceSessionController(
        launcher,
        token_store=LaunchTokenStore(
            token_factory=SequenceFactory("token-lifecycle-1", "token-lifecycle-2")
        ),
        session_id_factory=SequenceFactory("local-lifecycle-01", "local-lifecycle-02"),
        capability_broker=broker,
        browser_ack_timeout_seconds=0.05,
    )
    activation_client = FakeActivationClient(controller, "http://127.0.0.1:8765/voice")

    activation_task = activation_client.start()
    await asyncio.wait_for(activation_client.activation_started.wait(), timeout=0.2)
    activation = activation_client.activation
    assert activation is not None
    assert activation.status == "opened"
    assert activation.session_id == "local-lifecycle-01"
    assert activation.token == "token-lifecycle-1"
    assert len(launcher.launched_urls) == 1
    assert not activation_task.done()

    session_id = activation.session_id
    assert session_id is not None
    assert controller.validate_activation_token("token-lifecycle-1") == session_id
    assert await controller.consume_activation_token("token-lifecycle-1") == session_id
    assert await controller.consume_activation_token("token-lifecycle-1") is None

    await send_control(
        controller,
        session_id,
        PageReadyMessage(type="page_ready", session_id=session_id),
    )
    await send_control(
        controller,
        session_id,
        PageStartedMessage(type="page_started", session_id=session_id),
    )

    sideband_ws = FakeSidebandWebSocket()
    sideband = FakeSidebandConnector(sideband_ws)
    await controller.start_realtime_sideband(
        local_session_id=session_id,
        call_id="call_lifecycle_01",
        api_key="server-only-secret",
        websocket_connect=sideband,
    )
    assert len(sideband.calls) == 1
    assert sideband.calls[0][1] == {"Authorization": "Bearer server-only-secret"}

    tool_event = {
        "type": "response.function_call_arguments.done",
        "call_id": "call_time_lifecycle",
        "name": "assistant_get_current_time",
        "arguments": '{"timezone":"UTC"}',
    }
    await controller.process_sideband_event(session_id, tool_event)
    await controller.process_sideband_event(session_id, tool_event)
    assert broker.calls == [("assistant_get_current_time", {"timezone": "UTC"})]
    provider_events = [json.loads(payload) for payload in sideband_ws.sent]
    assert [event["type"] for event in provider_events] == [
        "conversation.item.create",
        "response.create",
    ]
    action_states = [
        await controller.wait_for_outbound_message(session_id),
        await controller.wait_for_outbound_message(session_id),
    ]
    assert all(isinstance(message, ActionStateMessage) for message in action_states)
    assert [message.state for message in action_states] == ["running", "completed"]

    await controller.begin_realtime_response(
        session_id,
        "resp-lifecycle-1",
        received_ns=100,
        provider_audio_start_ms=20,
    )
    for event in (
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-lifecycle-1",
            kind=InterruptionEventKind.SPEECH_STARTED,
            occurred_ns=1_000_000,
            user_speech_onset_ms=50.0,
        ),
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-lifecycle-1",
            kind=InterruptionEventKind.PLAYBACK_SUPPRESSED,
            occurred_ns=2_000_000,
        ),
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-lifecycle-1",
            kind=InterruptionEventKind.RESPONSE_CANCELLED,
            occurred_ns=3_000_000,
        ),
        InterruptionEvent(
            local_session_id=session_id,
            response_id="resp-lifecycle-1",
            kind=InterruptionEventKind.LISTENING_RESTORED,
            occurred_ns=4_000_000,
        ),
    ):
        assert await controller.record_interruption_event(session_id, event)
    await controller.begin_realtime_response(
        session_id,
        "resp-lifecycle-2",
        received_ns=5_000_000,
        provider_audio_start_ms=0,
    )
    traces = await controller.interruption_traces(session_id)
    assert len(traces) == 1
    assert traces[0].complete
    assert traces[0].speech_start_to_audible_silence_ms == 1.0

    await send_control(
        controller,
        session_id,
        StopMessage(type="stop", session_id=session_id, reason=StopReason.BUTTON),
    )
    server_stop = await controller.wait_for_outbound_message(session_id)
    assert server_stop == StopMessage(
        type="stop",
        session_id=session_id,
        reason=StopReason.BUTTON,
    )
    closed = await send_control(
        controller,
        session_id,
        TeardownCompleteMessage(type="teardown_complete", session_id=session_id),
    )
    transcript = await asyncio.wait_for(activation_task, timeout=0.2)

    assert closed is not None
    assert transcript.terminal is not None
    assert transcript.terminal.outcome is SessionOutcome.COMPLETED
    assert launcher.handles[0].close_calls == 1
    assert sideband_ws.closed
    assert controller.status == "idle"

    second = await controller.activate("http://127.0.0.1:8765/voice")
    assert second.status == "opened"
    assert second.session_id == "local-lifecycle-02"
    assert len(launcher.launched_urls) == 2
    await controller.close_active_session()
