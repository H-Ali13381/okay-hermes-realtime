"""Typed local-service boundaries for finalized Realtime capabilities.

These services are intentionally not registered with the model-facing capability
broker until real implementations and end-to-end tests exist.
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

JsonObject = dict[str, Any]


class ActionNotImplementedError(RuntimeError):
    """A finalized capability has a contract but no executable service yet."""


class StrictActionRequest(BaseModel):
    """Base request that rejects model- or caller-supplied fields outside its contract."""

    model_config = ConfigDict(extra="forbid")


class TimerStartRequest(StrictActionRequest):
    duration_seconds: int = Field(ge=1, le=86_400)
    label: str | None = Field(default=None, max_length=80)


class TimerCancelRequest(StrictActionRequest):
    timer_id: str | None = Field(default=None, min_length=1, max_length=128)


class MediaPlayRequest(StrictActionRequest):
    query: str = Field(min_length=1, max_length=200)
    media_type: Literal["music", "podcast", "any"] = "music"
    device: str | None = Field(default=None, max_length=80)


class MediaSkipRequest(StrictActionRequest):
    direction: Literal["next", "previous"] = "next"


class HermesTaskStartRequest(StrictActionRequest):
    task: str = Field(min_length=1, max_length=1_000)
    priority: Literal["normal", "high"] = "normal"


class HermesTaskReference(StrictActionRequest):
    task_id: str = Field(min_length=1, max_length=128)


def _not_implemented(action: str) -> NoReturn:
    raise ActionNotImplementedError(f"action is not implemented: {action}")


class TimerActions:
    """Local timer service boundary."""

    def start(self, _request: TimerStartRequest) -> JsonObject:
        _not_implemented("timer.start")

    def cancel(self, _request: TimerCancelRequest) -> JsonObject:
        _not_implemented("timer.cancel")


class MediaActions:
    """Local media service boundary."""

    def play(self, _request: MediaPlayRequest) -> JsonObject:
        _not_implemented("media.play")

    def pause(self) -> JsonObject:
        _not_implemented("media.pause")

    def resume(self) -> JsonObject:
        _not_implemented("media.resume")

    def skip(self, _request: MediaSkipRequest) -> JsonObject:
        _not_implemented("media.skip")


class HermesTaskActions:
    """Durable Hermes task service boundary."""

    def start(self, _request: HermesTaskStartRequest) -> JsonObject:
        _not_implemented("hermes.task.start")

    def cancel(self, _request: HermesTaskReference) -> JsonObject:
        _not_implemented("hermes.task.cancel")

    def status(self, _request: HermesTaskReference) -> JsonObject:
        _not_implemented("hermes.task.status")
