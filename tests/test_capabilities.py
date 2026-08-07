from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from realtime_action_spike.capabilities import (
    CapabilityBroker,
    ExecutionContractError,
    HermesAgentArguments,
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


def test_handoff_tool_contract_requires_direct_task_not_request() -> None:
    tool = next(tool for tool in build_openai_tools() if tool["name"] == "handoff_to_heavy_agent")
    parameters = tool["parameters"]

    assert parameters["required"] == ["task"]
    assert "task" in parameters["properties"]
    assert "request" not in parameters["properties"]
    description = tool["description"].casefold()
    assert "direct task" in description
    assert "routing language" in description
    assert "explicit consent" in description

    with pytest.raises(ExecutionContractError, match="unexpected argument: request"):
        CapabilityBroker().execute(
            "handoff_to_heavy_agent",
            {"request": "Change the wallpaper."},
        )


@pytest.mark.parametrize(
    "wrapped_task",
    [
        "Have Hermes audit the configuration.",
        "Ask Hermes to audit the configuration.",
        "Add a Kanban task to audit the configuration.",
        "Put this request on Kanban: audit the configuration.",
        "Send this to Hermes: audit the configuration.",
    ],
)
def test_handoff_contract_rejects_routing_wrappers(wrapped_task: str) -> None:
    with pytest.raises(ValidationError, match="direct task"):
        HermesAgentArguments.model_validate({"task": wrapped_task})


@pytest.mark.parametrize(
    "direct_task",
    [
        "Build a Kanban dashboard for this project.",
        "Audit Hermes Agent configuration for stale provider settings.",
    ],
)
def test_handoff_contract_preserves_literal_kanban_and_hermes_subjects(
    direct_task: str,
) -> None:
    assert HermesAgentArguments.model_validate({"task": direct_task}).task == direct_task


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
