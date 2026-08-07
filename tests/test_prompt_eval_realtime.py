from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from realtime_action_spike.prompt_eval import PromptOptCase
from realtime_action_spike.prompt_eval_realtime import (
    PromptCandidate,
    ProviderEventError,
    RealtimePromptEvaluator,
    classify_response_done,
)


class FakeWebSocket:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events: AsyncIterator[dict[str, Any]] = self._iterate(events)
        self.sent: list[dict[str, Any]] = []

    async def _iterate(self, events: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        for event in events:
            yield event

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        try:
            event = await anext(self._events)
        except StopAsyncIteration:
            await asyncio.Future()
            raise AssertionError("unreachable") from None
        return json.dumps(event)


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        return None


def _case(**overrides: object) -> PromptOptCase:
    payload: dict[str, object] = {
        "id": "explicit-handoff",
        "user_turns": ["Put this on Kanban: research AV1 support for my GPU."],
        "expected_action": "handoff",
        "expected_tool": "handoff_to_heavy_agent",
        "required_task_substrings": ["AV1", "GPU"],
        "forbidden_task_substrings": ["put this on kanban"],
        "routing_wrapper_exclusions": ["put this on kanban"],
        "tags": ["explicit_harmless_handoff"],
    }
    payload.update(overrides)
    return PromptOptCase.model_validate(payload)


def _candidate() -> PromptCandidate:
    return PromptCandidate(
        name="baseline",
        instructions="Use tools when appropriate.",
        tools=[
            {
                "type": "function",
                "name": "handoff_to_heavy_agent",
                "description": "Delegate a task.",
                "parameters": {
                    "type": "object",
                    "properties": {"request": {"type": "string"}},
                    "required": ["request"],
                },
            }
        ],
    )


def _done_with_call(arguments: dict[str, object]) -> dict[str, Any]:
    return {
        "type": "response.done",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "handoff_to_heavy_agent",
                    "arguments": json.dumps(arguments),
                }
            ],
        },
    }


def test_function_call_parser_accepts_baseline_request_and_never_executes_it() -> None:
    prediction = classify_response_done(_done_with_call({"request": "Research AV1 on GPU."}))

    assert prediction.action == "handoff"
    assert prediction.tool_name == "handoff_to_heavy_agent"
    assert prediction.task == "Research AV1 on GPU."


def test_text_parser_classifies_consent_confirmation_refusal_and_answer() -> None:
    def response(text: str) -> dict[str, Any]:
        return {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            },
        }

    handoff = classify_response_done(response("Want me to hand that to Hermes?"))
    consequence = classify_response_done(
        response("This will erase files. Should I proceed?")
    )
    assert classify_response_done(response("I can't help steal credentials.")).action == "refuse"
    answer = classify_response_done(
        response("Idempotent means repeated calls have the same effect.")
    )

    assert handoff.action == "ask_handoff"
    assert consequence.action == "ask_consequence"
    assert answer.action == "answer"


async def test_realtime_rollout_sends_exact_text_contract_and_returns_scored_result() -> None:
    websocket = FakeWebSocket(
        [
            {"type": "session.created", "session": {"id": "sess_ignored"}},
            {"type": "session.updated", "session": {"id": "sess_ignored"}},
            _done_with_call({"request": "Research AV1 support for my GPU."}),
        ]
    )
    connect_calls: list[tuple[str, dict[str, str]]] = []

    def connect(uri: str, *, additional_headers: dict[str, str]) -> FakeConnection:
        connect_calls.append((uri, additional_headers))
        return FakeConnection(websocket)

    evaluator = RealtimePromptEvaluator(
        api_key="secret-value-never-written",
        model="gpt-realtime-2.1",
        connect=connect,
        timeout_seconds=0.5,
    )
    result = await evaluator.rollout(_case(), _candidate())

    assert result.score.hard_pass is True
    assert result.provider_error is None
    assert result.case_id == "explicit-handoff"
    assert result.model == "gpt-realtime-2.1"
    assert "secret-value-never-written" not in result.model_dump_json()
    assert connect_calls == [
        (
            "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
            {"Authorization": "Bearer secret-value-never-written"},
        )
    ]

    assert [event["type"] for event in websocket.sent] == [
        "session.update",
        "conversation.item.create",
        "response.create",
    ]
    session = websocket.sent[0]["session"]
    assert session["type"] == "realtime"
    assert session["instructions"] == _candidate().instructions
    assert session["output_modalities"] == ["text"]
    assert session["tools"] == _candidate().tools
    assert session["tool_choice"] == "auto"
    assert websocket.sent[1]["item"]["content"] == [
        {"type": "input_text", "text": _case().user_turns[0]}
    ]
    assert websocket.sent[2] == {
        "type": "response.create",
        "response": {"output_modalities": ["text"]},
    }


async def test_provider_error_is_bounded_and_does_not_include_credentials() -> None:
    websocket = FakeWebSocket(
        [
            {"type": "session.created", "session": {}},
            {
                "type": "error",
                "error": {"message": "provider rejected event", "code": "bad_request"},
            },
        ]
    )

    evaluator = RealtimePromptEvaluator(
        api_key="top-secret",
        model="gpt-realtime-2.1",
        connect=lambda *_args, **_kwargs: FakeConnection(websocket),
        timeout_seconds=0.5,
    )

    with pytest.raises(ProviderEventError, match="provider rejected event") as error:
        await evaluator.rollout(_case(), _candidate())
    assert "top-secret" not in str(error.value)


async def test_rollout_times_out_when_provider_never_completes() -> None:
    websocket = FakeWebSocket([{"type": "session.created", "session": {}}])
    evaluator = RealtimePromptEvaluator(
        api_key="secret",
        model="gpt-realtime-2.1",
        connect=lambda *_args, **_kwargs: FakeConnection(websocket),
        timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError, match="Realtime rollout timed out"):
        await evaluator.rollout(_case(), _candidate())


def test_evaluator_rejects_missing_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        RealtimePromptEvaluator(api_key="", model="gpt-realtime-2.1")


def test_candidate_hash_is_stable_and_sensitive_to_contract() -> None:
    candidate = _candidate()
    same = _candidate()
    changed = _candidate().model_copy(update={"instructions": "Different instructions."})

    assert candidate.contract_hash == same.contract_hash
    assert candidate.contract_hash != changed.contract_hash
    assert len(candidate.contract_hash) == 64
