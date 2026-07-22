from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from realtime_action_spike.capabilities import ExecutionContractError, UnknownCapabilityError
from realtime_action_spike.openai.tool_loop import (
    ToolActionState,
    ToolCall,
    ToolCallConflictError,
    TrustedToolLoop,
)


@dataclass
class RecordingBroker:
    calls: list[tuple[str, str | dict[str, Any]]]

    def execute(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "assistant_get_current_time":
            return {
                "ok": True,
                "capability": name,
                "execution": "local",
                "result": {"spoken_time": "10:15 PM"},
            }
        if name == "voice_end_session":
            return {
                "ok": True,
                "capability": name,
                "execution": "local",
                "result": {"end_session": True},
            }
        if name == "malformed":
            raise ExecutionContractError("arguments must be valid JSON")
        raise UnknownCapabilityError(f"unknown capability: {name}")


async def make_loop() -> tuple[
    TrustedToolLoop,
    RecordingBroker,
    list[dict[str, Any]],
    list[ToolActionState],
]:
    broker = RecordingBroker([])
    provider_events: list[dict[str, Any]] = []
    action_states: list[ToolActionState] = []

    async def send_provider(event: dict[str, Any]) -> None:
        provider_events.append(event)

    async def publish_action(message: ToolActionState) -> None:
        action_states.append(message)

    loop = TrustedToolLoop(
        broker=broker,
        send_provider_event=send_provider,
        publish_action_state=publish_action,
    )
    return loop, broker, provider_events, action_states


@pytest.mark.asyncio
async def test_success_executes_once_and_sends_exact_output_then_continuation() -> None:
    loop, broker, provider_events, action_states = await make_loop()

    result = await loop.handle(
        ToolCall(
            call_id="call_time_01",
            name="assistant_get_current_time",
            arguments='{"timezone":"UTC"}',
        )
    )

    expected_output = {
        "call_id": "call_time_01",
        "ok": True,
        "capability": "assistant_get_current_time",
        "execution": "local",
        "result": {"spoken_time": "10:15 PM"},
    }
    assert broker.calls == [
        ("assistant_get_current_time", '{"timezone":"UTC"}')
    ]
    assert provider_events == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "call_time_01",
                "output": json.dumps(expected_output, separators=(",", ":")),
            },
        },
        {"type": "response.create"},
    ]
    assert result.output == expected_output
    assert result.close_after_farewell is False
    assert [message.state for message in action_states] == ["running", "completed"]
    assert all(not hasattr(message, "call_id") for message in action_states)


@pytest.mark.asyncio
async def test_canonical_duplicate_is_a_side_effect_free_replay() -> None:
    loop, broker, provider_events, action_states = await make_loop()
    first = ToolCall(
        call_id="call_duplicate_01",
        name="assistant_get_current_time",
        arguments='{"timezone":"UTC"}',
    )
    equivalent = ToolCall(
        call_id="call_duplicate_01",
        name="assistant_get_current_time",
        arguments='{ "timezone" : "UTC" }',
    )

    first_result = await loop.handle(first)
    replay_result = await loop.handle(equivalent)

    assert replay_result == first_result
    assert len(broker.calls) == 1
    assert len(provider_events) == 2
    assert len(action_states) == 2


@pytest.mark.asyncio
async def test_conflicting_duplicate_is_rejected_without_second_execution() -> None:
    loop, broker, provider_events, _action_states = await make_loop()
    await loop.handle(
        ToolCall(
            call_id="call_conflict_01",
            name="assistant_get_current_time",
            arguments='{"timezone":"UTC"}',
        )
    )

    with pytest.raises(ToolCallConflictError, match="call_id"):
        await loop.handle(
            ToolCall(
                call_id="call_conflict_01",
                name="assistant_get_current_time",
                arguments='{"timezone":"Europe/Paris"}',
            )
        )

    assert len(broker.calls) == 1
    assert len(provider_events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "error_type"),
    [
        ("malformed", "{not-json", "invalid_arguments"),
        ("delete_everything", "{}", "unknown_capability"),
    ],
)
async def test_broker_errors_are_returned_as_structured_function_output(
    name: str,
    arguments: str,
    error_type: str,
) -> None:
    loop, _broker, provider_events, action_states = await make_loop()

    result = await loop.handle(
        ToolCall(call_id=f"call_{error_type}", name=name, arguments=arguments)
    )

    assert result.output["ok"] is False
    assert result.output["error"]["type"] == error_type
    encoded_output = provider_events[0]["item"]["output"]
    assert json.loads(encoded_output) == result.output
    assert provider_events[1] == {"type": "response.create"}
    assert action_states[-1].state == "failed"
    assert action_states[-1].message in {
        "Capability arguments were rejected",
        "Capability is not available",
    }


@pytest.mark.asyncio
async def test_voice_end_session_sends_output_without_response_continuation() -> None:
    loop, broker, provider_events, action_states = await make_loop()

    result = await loop.handle(
        ToolCall(call_id="call_end_01", name="voice_end_session", arguments="{}")
    )

    assert broker.calls == [("voice_end_session", "{}")]
    assert len(provider_events) == 1
    assert provider_events[0]["type"] == "conversation.item.create"
    assert result.close_after_farewell is True
    assert action_states[-1].state == "closing"


@pytest.mark.asyncio
async def test_replay_resumes_partial_delivery_without_reexecution() -> None:
    broker = RecordingBroker([])
    provider_events: list[dict[str, Any]] = []
    action_states: list[ToolActionState] = []
    fail_continuation_once = True

    async def send_provider(event: dict[str, Any]) -> None:
        nonlocal fail_continuation_once
        if event == {"type": "response.create"} and fail_continuation_once:
            fail_continuation_once = False
            raise ConnectionError("sideband send failed")
        provider_events.append(event)

    async def publish_action(message: ToolActionState) -> None:
        action_states.append(message)

    loop = TrustedToolLoop(
        broker=broker,
        send_provider_event=send_provider,
        publish_action_state=publish_action,
    )
    call = ToolCall(
        call_id="call_resume_01",
        name="assistant_get_current_time",
        arguments='{"timezone":"UTC"}',
    )

    with pytest.raises(ConnectionError, match="sideband send failed"):
        await loop.handle(call)
    result = await loop.handle(call)

    assert result.output["ok"] is True
    assert len(broker.calls) == 1
    assert [event["type"] for event in provider_events] == [
        "conversation.item.create",
        "response.create",
    ]
    assert [message.state for message in action_states] == ["running", "completed"]
