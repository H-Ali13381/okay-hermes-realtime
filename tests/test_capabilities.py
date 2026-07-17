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
    "assistant_start_timer",
    "media_play",
    "media_control",
    "voice_end_session",
    "agent_delegate_task",
}


def fixed_now(_timezone) -> datetime:
    return datetime.fromisoformat("2026-07-17T15:04:05+00:00")


def test_tool_catalog_exposes_only_allowlisted_strict_function_schemas() -> None:
    tools = build_openai_tools()

    assert {tool["name"] for tool in tools} == EXPECTED_CAPABILITIES
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


def test_timer_execution_is_a_safe_simulation() -> None:
    output = CapabilityBroker().execute(
        "assistant_start_timer",
        {"duration_seconds": 300, "label": "tea"},
    )

    assert output["ok"] is True
    assert output["execution"] == "simulated"
    assert output["result"] == {
        "action": "timer.start",
        "duration_seconds": 300,
        "label": "tea",
        "status": "accepted_for_simulation",
    }


@pytest.mark.parametrize("duration", [0, 86_401])
def test_timer_rejects_out_of_bounds_durations(duration: int) -> None:
    with pytest.raises(ExecutionContractError, match="duration_seconds"):
        CapabilityBroker().execute(
            "assistant_start_timer",
            {"duration_seconds": duration},
        )


def test_media_control_requires_volume_for_set_volume() -> None:
    with pytest.raises(ExecutionContractError, match="volume_percent"):
        CapabilityBroker().execute("media_control", {"action": "set_volume"})


def test_media_control_rejects_volume_for_unrelated_action() -> None:
    with pytest.raises(ExecutionContractError, match="volume_percent"):
        CapabilityBroker().execute(
            "media_control",
            {"action": "pause", "volume_percent": 30},
        )


def test_delegate_task_simulation_has_stable_task_id() -> None:
    broker = CapabilityBroker()
    arguments = {"task": "Research native Linux notification APIs", "priority": "normal"}

    first = broker.execute("agent_delegate_task", arguments)
    second = broker.execute("agent_delegate_task", arguments)

    assert first == second
    assert first["execution"] == "simulated"
    assert first["result"]["task_id"].startswith("spike-")
    assert first["result"]["status"] == "accepted_for_simulation"


def test_unknown_capability_is_rejected() -> None:
    with pytest.raises(UnknownCapabilityError, match="delete_everything"):
        CapabilityBroker().execute("delete_everything", {})


def test_malformed_json_arguments_are_rejected() -> None:
    with pytest.raises(ExecutionContractError, match="valid JSON"):
        CapabilityBroker().execute("media_play", "{not-json}")


def test_non_object_json_arguments_are_rejected() -> None:
    with pytest.raises(ExecutionContractError, match="JSON object"):
        CapabilityBroker().execute("media_play", '["Daft Punk"]')


def test_extra_arguments_are_rejected() -> None:
    with pytest.raises(ExecutionContractError, match="unexpected"):
        CapabilityBroker().execute(
            "media_play",
            {"query": "Daft Punk", "shell_command": "rm -rf /"},
        )
