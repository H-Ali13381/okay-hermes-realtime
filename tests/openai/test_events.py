from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from realtime_action_spike.openai.events import (
    FunctionCallEventError,
    FunctionCallEventParser,
    FunctionCallRequest,
)


@pytest.fixture
def parser() -> FunctionCallEventParser:
    return FunctionCallEventParser()


def test_parser_ignores_non_function_call_events(parser: FunctionCallEventParser) -> None:
    assert parser.consume({"type": "response.audio_delta"}) == ()
    assert parser.consume({"type": "session.updated", "response": {"output": []}}) == ()


def test_delta_and_done_events_assemble_and_emit_immutable_request(
    parser: FunctionCallEventParser,
) -> None:
    assert (
        parser.consume(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
                "name": "assistant_get_current_time",
                "delta": '{"timezone":"local",',
            }
        )
        == ()
    )
    assert (
        parser.consume(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
                "name": "assistant_get_current_time",
                "delta": '"precision":"seconds"}',
            }
        )
        == ()
    )

    completed = parser.consume(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "assistant_get_current_time",
            "arguments": '{"timezone":"UTC"}',
        }
    )

    assert completed == (
        FunctionCallRequest(
            call_id="call_1",
            name="assistant_get_current_time",
            arguments={"timezone": "UTC"},
        ),
    )

    request = completed[0]
    assert isinstance(request, FunctionCallRequest)
    with pytest.raises(FrozenInstanceError):
        request.call_id = "mutated"


def test_response_done_event_provides_fallback_for_function_call_output(
    parser: FunctionCallEventParser,
) -> None:
    completed = parser.consume(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_from_done",
                        "name": "media_play",
                        "arguments": '{"query":"Daft Punk"}',
                    }
                ]
            },
        }
    )

    assert completed == (
        FunctionCallRequest(
            call_id="call_from_done",
            name="media_play",
            arguments={"query": "Daft Punk"},
        ),
    )


def test_final_arguments_override_partial_deltas(parser: FunctionCallEventParser) -> None:
    parser.consume(
        {
            "type": "response.function_call_arguments.delta",
            "call_id": "call_precise",
            "name": "media_control",
            "delta": '{"action":"pause"}',
        }
    )

    completed = parser.consume(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_precise",
                        "name": "media_control",
                        "arguments": '{"action":"next"}',
                    }
                ]
            },
        }
    )

    assert completed == (
        FunctionCallRequest(
            call_id="call_precise",
            name="media_control",
            arguments={"action": "next"},
        ),
    )


def test_duplicate_completed_call_ids_are_suppressed(parser: FunctionCallEventParser) -> None:
    event = {
        "type": "response.function_call_arguments.done",
        "call_id": "call_repeat",
        "name": "assistant_start_timer",
        "arguments": '{"duration_seconds":30}',
    }

    assert parser.consume(event) == (
        FunctionCallRequest(
            call_id="call_repeat",
            name="assistant_start_timer",
            arguments={"duration_seconds": 30},
        ),
    )
    assert parser.consume(event) == ()

    completed_from_done = parser.consume(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_repeat",
                        "name": "assistant_start_timer",
                        "arguments": '{"duration_seconds":30}',
                    }
                ]
            },
        }
    )

    assert completed_from_done == ()


def test_duplicate_call_id_with_different_payload_is_rejected(
    parser: FunctionCallEventParser,
) -> None:
    first = {
        "type": "response.function_call_arguments.done",
        "call_id": "call_conflict",
        "name": "media_play",
        "arguments": '{"query":"A"}',
    }
    parser.consume(first)

    conflict = {
        "type": "response.function_call_arguments.done",
        "call_id": "call_conflict",
        "name": "media_play",
        "arguments": '{"query":"B"}',
    }

    with pytest.raises(FunctionCallEventError, match="already completed"):
        parser.consume(conflict)


def test_malformed_events_are_rejected_without_leaking_provider_content(
    parser: FunctionCallEventParser,
) -> None:
    bad_json = '{"query":1'
    with pytest.raises(FunctionCallEventError) as exc:
        parser.consume(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_bad",
                "name": "media_play",
                "arguments": bad_json,
            }
        )
    assert bad_json not in str(exc.value)

    with pytest.raises(FunctionCallEventError, match="JSON object") as exc:
        parser.consume(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_bad2",
                "name": "media_play",
                "arguments": "[]",
            }
        )
    assert "[]" not in str(exc.value)

    with pytest.raises(FunctionCallEventError, match="call_id"):
        parser.consume(
            {
                "type": "response.function_call_arguments.done",
                "name": "media_play",
                "arguments": '{"query":"ok"}',
            }
        )

    with pytest.raises(FunctionCallEventError, match="delta"):
        parser.consume(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_bad3",
                "name": "media_play",
            }
        )


def test_oversized_argument_payload_is_rejected() -> None:
    parser = FunctionCallEventParser(max_argument_bytes=12)

    with pytest.raises(FunctionCallEventError, match="too large"):
        parser.consume(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_big",
                "name": "media_play",
                "arguments": '{"query":"excessively-long"}',
            }
        )

    assert parser.consume(
        {
            "type": "response.function_call_arguments.delta",
            "call_id": "call_big_delta",
            "name": "media_play",
            "delta": '{"query":',
        }
    ) == ()

    with pytest.raises(FunctionCallEventError, match="too large"):
        parser.consume(
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_big_delta",
                "name": "media_play",
                "delta": '{"value":"x"}',
            }
        )


def test_response_done_items_require_exact_function_call_fields() -> None:
    parser = FunctionCallEventParser()

    completed = parser.consume(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {"type": "function_call"},
                    {
                        "type": "function_call",
                        "call_id": "missing_name",
                        "arguments": '{"query":"ok"}',
                    },
                    {
                        "type": "function_call",
                        "call_id": "bad_args",
                        "name": "media_play",
                        "arguments": 42,
                    },
                    {
                        "type": "function_call",
                        "call_id": "bad_name",
                        "name": "media_control",
                        "arguments": '"not-object"',
                    },
                ]
            },
        }
    )

    assert completed == ()

    assert parser.consume(
        {
            "type": "response.done",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "good",
                        "name": "media_play",
                        "arguments": '{"query":"valid"}',
                    }
                ]
            },
        }
    ) == (
        FunctionCallRequest(
            call_id="good",
            name="media_play",
            arguments={"query": "valid"},
        ),
    )
