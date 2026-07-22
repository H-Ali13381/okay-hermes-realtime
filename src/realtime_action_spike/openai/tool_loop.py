from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from realtime_action_spike.capabilities import ExecutionContractError, UnknownCapabilityError

_MAX_CALLS_PER_SESSION = 512
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
ToolActionStatus = Literal["running", "completed", "failed", "closing"]


class CapabilityExecutor(Protocol):
    def execute(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]: ...


class ToolCallError(ValueError):
    """Base class for rejected trusted-sideband tool calls."""


class ToolCallConflictError(ToolCallError):
    """A provider call ID was reused with different request content."""


class ToolCallLimitError(ToolCallError):
    """The bounded per-session tool-call registry is full."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str | dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolActionState:
    capability: str
    state: ToolActionStatus
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output_json: str
    close_after_farewell: bool

    @property
    def output(self) -> dict[str, Any]:
        parsed = json.loads(self.output_json)
        assert isinstance(parsed, dict)
        return parsed


@dataclass(slots=True)
class _ExecutionRecord:
    fingerprint: str
    result: ToolExecutionResult
    output_sent: bool = False
    continuation_sent: bool = False
    action_state_sent: bool = False


class TrustedToolLoop:
    """Execute completed Realtime function calls once on the trusted controller side."""

    def __init__(
        self,
        *,
        broker: CapabilityExecutor,
        send_provider_event: Callable[[dict[str, Any]], Awaitable[None]],
        publish_action_state: Callable[[ToolActionState], Awaitable[None]],
        max_calls: int = _MAX_CALLS_PER_SESSION,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self._broker = broker
        self._send_provider_event = send_provider_event
        self._publish_action_state = publish_action_state
        self._max_calls = max_calls
        self._records: dict[str, _ExecutionRecord] = {}
        self._lock = asyncio.Lock()

    async def handle(self, call: ToolCall) -> ToolExecutionResult:
        _validate_call(call)
        fingerprint = _request_fingerprint(call)

        async with self._lock:
            record = self._records.get(call.call_id)
            if record is not None:
                if record.fingerprint != fingerprint:
                    raise ToolCallConflictError(
                        "OpenAI call_id was already used with a different request"
                    )
                await self._deliver(call, record)
                return record.result

            if len(self._records) >= self._max_calls:
                raise ToolCallLimitError("Realtime session reached its execution safety limit")

            await self._publish_action_state(
                ToolActionState(
                    capability=call.name,
                    state="running",
                )
            )
            output, final_state, final_message = self._execute(call)
            close_after_farewell = bool(
                output.get("ok")
                and call.name == "voice_end_session"
                and isinstance(output.get("result"), dict)
                and output["result"].get("end_session") is True
            )
            if close_after_farewell:
                final_state = "closing"
                final_message = "Ending voice session"

            result = ToolExecutionResult(
                output_json=json.dumps(output, separators=(",", ":")),
                close_after_farewell=close_after_farewell,
            )
            record = _ExecutionRecord(fingerprint=fingerprint, result=result)
            self._records[call.call_id] = record
            await self._deliver(
                call,
                record,
                final_state=final_state,
                final_message=final_message,
            )
            return result

    def _execute(self, call: ToolCall) -> tuple[dict[str, Any], ToolActionStatus, str]:
        try:
            broker_result = self._broker.execute(call.name, call.arguments)
        except UnknownCapabilityError as exc:
            return (
                {
                    "call_id": call.call_id,
                    "ok": False,
                    "error": {"type": "unknown_capability", "message": str(exc)},
                },
                "failed",
                "Capability is not available",
            )
        except ExecutionContractError as exc:
            return (
                {
                    "call_id": call.call_id,
                    "ok": False,
                    "error": {"type": "invalid_arguments", "message": str(exc)},
                },
                "failed",
                "Capability arguments were rejected",
            )

        return (
            {"call_id": call.call_id, **broker_result},
            "completed",
            "Capability completed",
        )

    async def _deliver(
        self,
        call: ToolCall,
        record: _ExecutionRecord,
        *,
        final_state: ToolActionStatus | None = None,
        final_message: str | None = None,
    ) -> None:
        if not record.output_sent:
            await self._send_provider_event(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": record.result.output_json,
                    },
                }
            )
            record.output_sent = True

        if not record.result.close_after_farewell and not record.continuation_sent:
            await self._send_provider_event({"type": "response.create"})
            record.continuation_sent = True

        if not record.action_state_sent:
            output = record.result.output
            if final_state is None:
                if record.result.close_after_farewell:
                    final_state = "closing"
                    final_message = "Ending voice session"
                elif output.get("ok") is True:
                    final_state = "completed"
                    final_message = "Capability completed"
                elif output.get("error", {}).get("type") == "invalid_arguments":
                    final_state = "failed"
                    final_message = "Capability arguments were rejected"
                else:
                    final_state = "failed"
                    final_message = "Capability is not available"
            await self._publish_action_state(
                ToolActionState(
                    capability=call.name,
                    state=final_state,
                    message=final_message,
                )
            )
            record.action_state_sent = True


def _validate_call(call: ToolCall) -> None:
    if _CALL_ID_RE.fullmatch(call.call_id) is None:
        raise ToolCallError("call_id must be a bounded provider identifier")
    if _CAPABILITY_RE.fullmatch(call.name) is None:
        raise ToolCallError("name must be a bounded capability identifier")
    if not isinstance(call.arguments, (str, dict)):
        raise ToolCallError("arguments must be JSON text or an object")


def _request_fingerprint(call: ToolCall) -> str:
    arguments: Any = call.arguments
    if isinstance(arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            arguments = json.loads(arguments)
    canonical = json.dumps(
        {"name": call.name, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
