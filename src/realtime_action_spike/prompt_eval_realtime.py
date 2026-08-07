"""Text-only OpenAI Realtime rollouts for voice prompt optimization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import connect as websocket_connect

from .prompt_eval import (
    PromptOptAction,
    PromptOptCase,
    PromptOptPrediction,
    PromptOptScore,
    score_promptopt_case,
)

_MAX_ERROR_LENGTH = 512


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderEventError(RuntimeError):
    """The Realtime provider returned an explicit or malformed error event."""


class PromptCandidate(_StrictModel):
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    tools: list[dict[str, Any]]

    @property
    def contract_hash(self) -> str:
        canonical = json.dumps(
            {"instructions": self.instructions, "tools": self.tools},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RealtimeRolloutResult(_StrictModel):
    case_id: str
    candidate_name: str
    candidate_hash: str
    model: str
    prediction: PromptOptPrediction
    score: PromptOptScore
    elapsed_seconds: float = Field(ge=0)
    provider_error: str | None


def _bounded_error(value: object) -> str:
    normalized = " ".join(str(value).split())
    return normalized[:_MAX_ERROR_LENGTH]


def _text_from_output(output: list[object]) -> str:
    text_parts: list[str] = []
    for raw_item in output:
        if not isinstance(raw_item, dict) or raw_item.get("type") != "message":
            continue
        content = raw_item.get("content")
        if not isinstance(content, list):
            continue
        for raw_content in content:
            if not isinstance(raw_content, dict):
                continue
            if raw_content.get("type") == "output_text" and isinstance(
                raw_content.get("text"), str
            ):
                text_parts.append(raw_content["text"])
    return " ".join(text_parts).strip()


def _classify_text(text: str) -> PromptOptAction:
    folded = text.casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    is_question = "?" in text
    consequence_terms = (
        "delete",
        "erase",
        "overwrite",
        "permanent",
        "will remove",
        "will disappear",
        "should i proceed",
        "are you sure",
        "confirm",
    )
    if is_question and any(term in folded for term in consequence_terms):
        return PromptOptAction.ask_consequence
    if is_question and any(
        term in folded for term in ("hermes", "hand that", "hand this", "kanban")
    ):
        return PromptOptAction.ask_handoff
    if any(
        term in folded
        for term in ("i can't", "i cannot", "i won't", "unable to", "not able to")
    ):
        return PromptOptAction.refuse
    return PromptOptAction.answer


def classify_response_done(event: dict[str, Any]) -> PromptOptPrediction:
    """Convert one completed Realtime response into a deterministic prediction."""

    if event.get("type") != "response.done":
        raise ProviderEventError("expected response.done event")
    response = event.get("response")
    if not isinstance(response, dict):
        raise ProviderEventError("response.done is missing response data")
    if response.get("status") != "completed":
        details = response.get("status_details") or response.get("status") or "unknown"
        raise ProviderEventError(f"Realtime response did not complete: {_bounded_error(details)}")
    output = response.get("output")
    if not isinstance(output, list):
        raise ProviderEventError("response.done is missing output")

    for raw_item in output:
        if not isinstance(raw_item, dict) or raw_item.get("type") != "function_call":
            continue
        name = raw_item.get("name")
        arguments_text = raw_item.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments_text, str):
            raise ProviderEventError("function call is missing name or arguments")
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError as exc:
            raise ProviderEventError("function call arguments are not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ProviderEventError("function call arguments must be a JSON object")
        action = (
            PromptOptAction.handoff
            if name == "handoff_to_heavy_agent"
            else PromptOptAction.direct_tool
        )
        task = arguments.get("task", arguments.get("request"))
        return PromptOptPrediction(
            action=action,
            tool_name=name,
            task=task if isinstance(task, str) else None,
        )

    text = _text_from_output(output)
    if not text:
        raise ProviderEventError("completed response contains no text or function call")
    return PromptOptPrediction(action=_classify_text(text), assistant_text=text)


class RealtimePromptEvaluator:
    """Run isolated text turns against OpenAI Realtime without executing tools."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        connect: Callable[..., Any] = websocket_connect,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for Realtime evaluation")
        if not model.strip():
            raise ValueError("Realtime model is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key
        self._model = model
        self._connect = connect
        self._timeout_seconds = timeout_seconds

    async def _receive_event(self, websocket: Any) -> dict[str, Any]:
        payload = await websocket.recv()
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        try:
            event = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderEventError("provider returned invalid JSON") from exc
        if not isinstance(event, dict):
            raise ProviderEventError("provider event must be a JSON object")
        if event.get("type") == "error":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise ProviderEventError(_bounded_error(message or "unknown provider error"))
        return event

    async def _receive_until(self, websocket: Any, event_type: str) -> dict[str, Any]:
        while True:
            event = await self._receive_event(websocket)
            if event.get("type") == event_type:
                return event

    async def _rollout(
        self,
        case: PromptOptCase,
        candidate: PromptCandidate,
    ) -> PromptOptPrediction:
        uri = "wss://api.openai.com/v1/realtime?" + urlencode({"model": self._model})
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with self._connect(uri, additional_headers=headers) as websocket:
            await self._receive_until(websocket, "session.created")
            await websocket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": candidate.instructions,
                            "output_modalities": ["text"],
                            "tools": candidate.tools,
                            "tool_choice": "auto",
                        },
                    }
                )
            )
            await self._receive_until(websocket, "session.updated")

            prediction: PromptOptPrediction | None = None
            for user_turn in case.user_turns:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": user_turn}],
                            },
                        }
                    )
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.create",
                            "response": {"output_modalities": ["text"]},
                        }
                    )
                )
                event = await self._receive_until(websocket, "response.done")
                prediction = classify_response_done(event)

            if prediction is None:
                raise ProviderEventError("case contains no user turns")
            return prediction

    async def rollout(
        self,
        case: PromptOptCase,
        candidate: PromptCandidate,
    ) -> RealtimeRolloutResult:
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                prediction = await self._rollout(case, candidate)
        except TimeoutError as exc:
            raise TimeoutError("Realtime rollout timed out") from exc
        elapsed = time.monotonic() - started
        return RealtimeRolloutResult(
            case_id=case.id,
            candidate_name=candidate.name,
            candidate_hash=candidate.contract_hash,
            model=self._model,
            prediction=prediction,
            score=score_promptopt_case(case, prediction),
            elapsed_seconds=elapsed,
            provider_error=None,
        )
