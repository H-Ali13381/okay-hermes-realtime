"""Safe typed capability contract for the Realtime function-calling spike."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError

JsonObject = dict[str, Any]
NowProvider = Callable[[ZoneInfo | None], datetime]


class ExecutionContractError(ValueError):
    """The model proposed arguments outside the allowlisted contract."""


class UnknownCapabilityError(ExecutionContractError):
    """The model requested a capability that was not exposed."""


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CurrentTimeArguments(StrictArguments):
    timezone: str = Field(
        default="local",
        min_length=1,
        max_length=80,
        description="IANA timezone such as UTC or America/Toronto. Use local when unspecified.",
    )

class EndSessionArguments(StrictArguments):
    reason: str | None = Field(default=None, max_length=120)


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    name: str
    description: str
    arguments_model: type[StrictArguments]
    execution: Literal["local", "simulated"]
    handler: Callable[[StrictArguments, NowProvider], JsonObject]


def _default_now(timezone: ZoneInfo | None) -> datetime:
    if timezone is None:
        return datetime.now().astimezone()
    return datetime.now(timezone)


def _get_current_time(arguments: StrictArguments, now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, CurrentTimeArguments)
    requested_timezone = arguments.timezone
    try:
        timezone = None if requested_timezone == "local" else ZoneInfo(requested_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ExecutionContractError(f"unknown timezone: {requested_timezone}") from exc

    current = now_provider(timezone)
    resolved_timezone = requested_timezone
    if requested_timezone == "local":
        resolved_timezone = str(current.tzinfo or "local")

    return {
        "timezone": resolved_timezone,
        "iso_time": current.isoformat(),
        "spoken_time": current.strftime("%-I:%M %p"),
    }


def _end_session(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, EndSessionArguments)
    return {
        "action": "voice.end_session",
        "end_session": True,
        "reason": arguments.reason,
        "status": "accepted_locally",
    }


CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition(
        name="assistant_get_current_time",
        description=(
            "Return the user's current local time or the current time in an explicit IANA "
            "timezone. Call this for current-time questions instead of guessing."
        ),
        arguments_model=CurrentTimeArguments,
        execution="local",
        handler=_get_current_time,
    ),
    CapabilityDefinition(
        name="voice_end_session",
        description=(
            "End this voice session when the user explicitly asks to stop, disconnect, or end "
            "the conversation."
        ),
        arguments_model=EndSessionArguments,
        execution="local",
        handler=_end_session,
    ),
)

_CAPABILITIES_BY_NAME = {capability.name: capability for capability in CAPABILITIES}


def build_openai_tools() -> list[JsonObject]:
    """Return provider-facing function definitions generated from the execution contract."""
    return [
        {
            "type": "function",
            "name": capability.name,
            "description": capability.description,
            "parameters": capability.arguments_model.model_json_schema(),
        }
        for capability in CAPABILITIES
    ]


def _parse_arguments(arguments: str | Mapping[str, Any]) -> JsonObject:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ExecutionContractError("arguments must be valid JSON") from exc
    elif isinstance(arguments, Mapping):
        parsed = dict(arguments)
    else:
        raise ExecutionContractError("arguments must be a JSON object")

    if not isinstance(parsed, dict):
        raise ExecutionContractError("arguments must be a JSON object")
    return parsed


def _format_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        if error["type"] == "extra_forbidden":
            messages.append(f"unexpected argument: {location}")
        else:
            messages.append(f"{location}: {error['msg']}")
    return "; ".join(messages)


class CapabilityBroker:
    """Validate and execute only capabilities present in the static allowlist."""

    def __init__(self, now_provider: NowProvider = _default_now) -> None:
        self._now_provider = now_provider

    def execute(self, name: str, arguments: str | Mapping[str, Any]) -> JsonObject:
        capability = _CAPABILITIES_BY_NAME.get(name)
        if capability is None:
            raise UnknownCapabilityError(f"unknown capability: {name}")

        parsed = _parse_arguments(arguments)
        try:
            validated = capability.arguments_model.model_validate(parsed)
        except ValidationError as exc:
            raise ExecutionContractError(_format_validation_error(exc)) from exc

        result = capability.handler(validated, self._now_provider)
        return {
            "ok": True,
            "capability": capability.name,
            "execution": capability.execution,
            "result": result,
        }
