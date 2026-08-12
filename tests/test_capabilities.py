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
    parse_media_decision_arguments,
    parse_media_prepare_anime_arguments,
    parse_media_prepare_youtube_arguments,
)

EXPECTED_CAPABILITIES = {
    "assistant_get_current_time",
    "voice_end_session",
    "handoff_to_heavy_agent",
    "check_heavy_agent_task",
    "resolve_heavy_agent_block",
    "media_prepare_youtube",
    "media_prepare_anime",
    "media_decide",
}


def non_null_schema(schema: dict[str, object]) -> dict[str, object]:
    variants = schema.get("anyOf")
    if not isinstance(variants, list):
        return schema
    return next(
        variant
        for variant in variants
        if isinstance(variant, dict) and variant.get("type") != "null"
    )


def fixed_now(_timezone) -> datetime:
    return datetime.fromisoformat("2026-07-17T15:04:05+00:00")


def test_tool_catalog_exposes_all_capabilities() -> None:
    tools = build_openai_tools()

    assert {tool["name"] for tool in tools} == EXPECTED_CAPABILITIES
    assert len(tools) == 8
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
        "Please have Hermes review the API document.",
        "Research AV1 support for my GPU and have Hermes do it.",
        "Research AV1 support for my GPU, then send it to Hermes.",
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
        "Document how to have Hermes Agent review API changes safely.",
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


def test_media_prepare_youtube_contract_is_strict_and_requires_live_session_binding() -> None:
    tool = next(tool for tool in build_openai_tools() if tool["name"] == "media_prepare_youtube")

    assert tool["parameters"]["required"] == ["query"]
    assert list(tool["parameters"]["properties"]) == ["query"]
    assert tool["parameters"]["additionalProperties"] is False
    query = tool["parameters"]["properties"]["query"]
    assert query["type"] == "string"
    assert query["minLength"] == 1
    assert query["maxLength"] >= 1
    description = tool["description"].casefold()
    assert "spoken" in description
    assert "interpret" in description
    assert "yep, that's it" in description
    assert "do not ask" in description
    assert "live session" in description

    with pytest.raises(ExecutionContractError, match="originating live voice session"):
        CapabilityBroker().execute("media_prepare_youtube", {"query": "latest useful video"})


def test_media_prepare_anime_contract_requires_episode_and_offset_controls() -> None:
    tool = next(tool for tool in build_openai_tools() if tool["name"] == "media_prepare_anime")

    assert tool["parameters"]["required"] == ["query"]
    props = tool["parameters"]["properties"]
    assert list(props) == ["query", "episode", "episode_offset", "audio_mode"]
    assert props["query"]["type"] == "string"
    assert props["query"]["minLength"] == 1
    episode = non_null_schema(props["episode"])
    episode_offset = non_null_schema(props["episode_offset"])
    assert episode["type"] == "integer"
    assert episode["minimum"] == 1
    assert episode_offset["type"] == "integer"
    assert episode_offset["minimum"] <= -1
    assert episode_offset["maximum"] >= 1
    assert props["audio_mode"]["enum"] == ["auto", "sub", "dub"]
    description = tool["description"].casefold()
    assert "interpret" in description
    assert "compound" in description
    assert "next episode" in description

    with pytest.raises(ExecutionContractError, match="originating live voice session"):
        CapabilityBroker().execute(
            "media_prepare_anime",
            {"query": "new series", "episode": 1, "audio_mode": "auto"},
        )

    with pytest.raises(ExecutionContractError, match="mutually exclusive"):
        CapabilityBroker().execute(
            "media_prepare_anime",
            {"query": "new series", "episode": 1, "episode_offset": 1},
        )


def test_media_decide_contract_accepts_only_revise_with_revision_details() -> None:
    tool = next(tool for tool in build_openai_tools() if tool["name"] == "media_decide")

    props = tool["parameters"]["properties"]
    assert tool["parameters"]["required"] == ["pending_id", "action"]
    assert list(props) == ["pending_id", "action", "revision"]
    assert props["pending_id"]["type"] == "string"
    assert props["pending_id"]["pattern"]
    assert props["action"]["enum"] == ["accept", "reject", "cancel", "revise"]
    assert "revision" in props
    revision_ref = non_null_schema(props["revision"])["$ref"]
    assert isinstance(revision_ref, str)
    revision = tool["parameters"]["$defs"][revision_ref.rsplit("/", 1)[1]]
    assert revision["type"] == "object"
    assert revision["additionalProperties"] is False
    revision_query = non_null_schema(revision["properties"]["query"])
    revision_offset = non_null_schema(revision["properties"]["episode_offset"])
    revision_audio = non_null_schema(revision["properties"]["audio_mode"])
    assert revision_query["type"] == "string"
    assert revision_offset["minimum"] == -100
    assert revision_offset["maximum"] == 100
    assert revision_audio["enum"] == ["auto", "sub", "dub"]
    description = tool["description"].casefold()
    assert "conversational replies" in description
    assert "no, play the next episode" in description
    assert "revision" in description

    with pytest.raises(ExecutionContractError, match="originating live voice session"):
        CapabilityBroker().execute(
            "media_decide",
            {"pending_id": "pending_01", "action": "accept"},
        )


def test_media_decide_rejects_revision_fields_when_not_revise() -> None:
    with pytest.raises(ExecutionContractError, match="revision"):
        CapabilityBroker().execute(
            "media_decide",
            {
                "pending_id": "pending_01",
                "action": "accept",
                "revision": {"query": "new title"},
            },
        )


def test_media_decide_revision_requires_values_when_revise() -> None:
    with pytest.raises(ExecutionContractError, match="revision"):
        CapabilityBroker().execute(
            "media_decide",
            {"pending_id": "pending_01", "action": "revise", "revision": {}},
        )


def test_media_parser_helpers_return_strict_typed_arguments() -> None:
    youtube = parse_media_prepare_youtube_arguments({"query": "  latest useful video  "})
    anime = parse_media_prepare_anime_arguments(
        {"query": "  Cowboy Bebop  ", "episode": 2, "audio_mode": "sub"}
    )
    decision = parse_media_decision_arguments(
        {
            "pending_id": "pending_01",
            "action": "revise",
            "revision": {"episode_offset": 1},
        }
    )

    assert youtube.query == "latest useful video"
    assert anime.query == "Cowboy Bebop"
    assert anime.episode == 2
    assert decision.revision is not None
    assert decision.revision.episode_offset == 1


@pytest.mark.parametrize(
    "query",
    ["3.11 release overview", "A.V. Club review", "m.youtube.com/watch?v=dQw4w9WgXcQ"],
)
def test_youtube_plain_search_text_is_not_misclassified_as_a_url(query: str) -> None:
    parsed = parse_media_prepare_youtube_arguments({"query": query})

    assert parsed.query == query


@pytest.mark.parametrize(
    "query",
    [
        "http://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://example.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_youtube_explicit_unsupported_urls_are_rejected(query: str) -> None:
    with pytest.raises(ExecutionContractError):
        parse_media_prepare_youtube_arguments({"query": query})


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
