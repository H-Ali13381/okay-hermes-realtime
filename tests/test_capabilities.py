from __future__ import annotations

from datetime import datetime

import pytest

from realtime_action_spike.capabilities import (
    CapabilityBroker,
    ExecutionContractError,
    UnknownCapabilityError,
    build_openai_tools,
)

EXPECTED_CAPABILITIES = {
    "assistant_get_current_time",
    "voice_end_session",
    "handoff_to_heavy_agent",
    "check_heavy_agent_task",
    "resolve_heavy_agent_block",
}


def fixed_now(_timezone) -> datetime:
    return datetime.fromisoformat("2026-07-17T15:04:05+00:00")


def test_tool_catalog_exposes_all_capabilities() -> None:
    tools = build_openai_tools()

    assert {tool["name"] for tool in tools} == EXPECTED_CAPABILITIES
    assert len(tools) == 5
    assert all(tool["type"] == "function" for tool in tools)
    assert all(tool["description"].strip() for tool in tools)
    assert all(tool["parameters"]["type"] == "object" for tool in tools)
    assert all(tool["parameters"]["additionalProperties"] is False for tool in tools)


def test_current_time_executes_locally_with_explicit_timezone() -> None:
    broker = CapabilityBroker(now_provider=fixed_now)

    output = broker.execute("assistant_get_current_time", '{"timezone":"UTC"}')

    assert output == {
        "ok": True,
        "capability": "assistant_get_current_time",
        "execution": "local",
        "result": {
            "timezone": "UTC",
            "iso_time": "2026-07-17T15:04:05+00:00",
            "spoken_time": "3:04 PM",
        },
    }


def test_voice_end_session_reports_local_lifecycle_intent() -> None:
    output = CapabilityBroker().execute("voice_end_session", {"reason": "user requested stop"})

    assert output == {
        "ok": True,
        "capability": "voice_end_session",
        "execution": "local",
        "result": {
            "action": "voice.end_session",
            "end_session": True,
            "reason": "user requested stop",
            "status": "accepted_locally",
        },
    }


@pytest.mark.parametrize(
    "capability_name",
    ["assistant_start_timer", "media_play", "media_control", "agent_delegate_task"],
)
def test_disallowed_stage1_capabilities_are_not_in_normal_catalog(capability_name: str) -> None:
    with pytest.raises(UnknownCapabilityError, match=capability_name):
        CapabilityBroker().execute(capability_name, {})


def test_permission_resolution_cannot_bypass_live_session_binding() -> None:
    with pytest.raises(ExecutionContractError, match="originating live voice session"):
        CapabilityBroker().execute(
            "resolve_heavy_agent_block",
            {
                "task_id": "t_voice01",
                "block_event_id": 17,
                "decision": "approve_once",
                "response": "yes",
            },
        )


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(UnknownCapabilityError, match="delete_everything"):
        CapabilityBroker().execute("delete_everything", {})


def test_malformed_json_arguments_are_rejected() -> None:
    with pytest.raises(ExecutionContractError, match="valid JSON"):
        CapabilityBroker().execute("assistant_get_current_time", "{not-json}")


def test_non_object_json_arguments_are_rejected() -> None:
    with pytest.raises(ExecutionContractError, match="JSON object"):
        CapabilityBroker().execute("assistant_get_current_time", '["Daft Punk"]')


def test_end_session_does_not_accept_extraneous_arguments() -> None:
    with pytest.raises(ExecutionContractError, match="unexpected argument"):
        CapabilityBroker().execute(
            "voice_end_session",
            {"reason": "done", "unexpected": "nope"},
        )
