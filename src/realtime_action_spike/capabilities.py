"""Safe typed capability contract for the Realtime function-calling spike."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class StartTimerArguments(StrictArguments):
    duration_seconds: int = Field(
        ge=1,
        le=86_400,
        description="Timer duration in whole seconds, from 1 second through 24 hours.",
    )
    label: str | None = Field(default=None, max_length=80)


class MediaPlayArguments(StrictArguments):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Song, artist, album, playlist, or podcast requested by the user.",
    )
    media_type: Literal["music", "podcast", "any"] = "music"
    device: str | None = Field(default=None, max_length=80)


class MediaControlArguments(StrictArguments):
    action: Literal["play", "pause", "resume", "next", "previous", "stop", "set_volume"]
    volume_percent: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_volume_contract(self) -> MediaControlArguments:
        if self.action == "set_volume" and self.volume_percent is None:
            raise ValueError("volume_percent is required when action is set_volume")
        if self.action != "set_volume" and self.volume_percent is not None:
            raise ValueError("volume_percent is only valid when action is set_volume")
        return self


class EndSessionArguments(StrictArguments):
    reason: str | None = Field(default=None, max_length=120)


class DelegateTaskArguments(StrictArguments):
    task: str = Field(min_length=1, max_length=1_000)
    priority: Literal["normal", "high"] = "normal"


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


def _start_timer(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, StartTimerArguments)
    return {
        "action": "timer.start",
        "duration_seconds": arguments.duration_seconds,
        "label": arguments.label,
        "status": "accepted_for_simulation",
    }


def _media_play(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, MediaPlayArguments)
    return {
        "action": "media.play",
        "query": arguments.query,
        "media_type": arguments.media_type,
        "device": arguments.device,
        "status": "accepted_for_simulation",
    }


def _media_control(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, MediaControlArguments)
    return {
        "action": f"media.{arguments.action}",
        "volume_percent": arguments.volume_percent,
        "status": "accepted_for_simulation",
    }


def _end_session(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, EndSessionArguments)
    return {
        "action": "voice.end_session",
        "end_session": True,
        "reason": arguments.reason,
        "status": "accepted_for_simulation",
    }


def _delegate_task(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, DelegateTaskArguments)
    stable_input = f"{arguments.priority}\0{arguments.task}".encode()
    task_id = f"spike-{hashlib.sha256(stable_input).hexdigest()[:12]}"
    return {
        "action": "agent.delegate_task",
        "task": arguments.task,
        "priority": arguments.priority,
        "task_id": task_id,
        "status": "accepted_for_simulation",
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
        name="assistant_start_timer",
        description=(
            "Start a timer after the user clearly asks for one and provides a duration. "
            "This spike simulates the timer instead of changing the operating system."
        ),
        arguments_model=StartTimerArguments,
        execution="simulated",
        handler=_start_timer,
    ),
    CapabilityDefinition(
        name="media_play",
        description=(
            "Play requested music or a podcast. Call only for an explicit playback request, "
            "not when merely discussing an artist or song. This spike simulates playback."
        ),
        arguments_model=MediaPlayArguments,
        execution="simulated",
        handler=_media_play,
    ),
    CapabilityDefinition(
        name="media_control",
        description=(
            "Control current media playback: play, pause, resume, next, previous, stop, or set "
            "volume. This spike simulates the control action."
        ),
        arguments_model=MediaControlArguments,
        execution="simulated",
        handler=_media_control,
    ),
    CapabilityDefinition(
        name="voice_end_session",
        description=(
            "End this voice session when the user explicitly asks to stop, disconnect, or end "
            "the conversation."
        ),
        arguments_model=EndSessionArguments,
        execution="simulated",
        handler=_end_session,
    ),
    CapabilityDefinition(
        name="agent_delegate_task",
        description=(
            "Hand off a clearly requested multi-step research, coding, file, or automation task "
            "to the deeper assistant. This spike records a simulated handoff only."
        ),
        arguments_model=DelegateTaskArguments,
        execution="simulated",
        handler=_delegate_task,
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
