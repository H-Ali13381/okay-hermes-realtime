"""Safe typed capability contract for the Realtime function-calling spike."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .routing import find_routing_wrappers

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


class HermesAgentArguments(StrictArguments):
    task: str = Field(
        min_length=1,
        max_length=4_000,
        description=(
            "Direct, complete, self-contained task for the full Hermes Agent. State the work "
            "itself, preserve every user constraint, and omit routing language such as "
            "'Have Hermes', 'add a Kanban task', or 'put this on Kanban'."
        ),
    )

    @field_validator("task")
    @classmethod
    def _task_is_transport_neutral(cls, value: str) -> str:
        if find_routing_wrappers(value):
            raise ValueError(
                "state the direct task itself without Hermes or Kanban routing language"
            )
        return value


class TaskStatusArguments(StrictArguments):
    task_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Kanban task id from a previous handoff result. "
            "Omit to check the most recent handoff."
        ),
    )


class PermissionResolutionArguments(StrictArguments):
    task_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Exact Kanban task id from the permission request.",
    )
    block_event_id: int = Field(
        ge=1,
        description="Exact task_events id carried by the permission request.",
    )
    decision: Literal["approve_once", "deny"] = Field(
        description="One-shot approval or denial. Blanket approval is not supported.",
    )
    response: str = Field(
        min_length=1,
        max_length=500,
        description="The user's explicit answer, without inferred or expanded permissions.",
    )


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    name: str
    description: str
    arguments_model: type[StrictArguments]
    execution: Literal["local", "simulated", "kanban"]
    handler: Callable[[StrictArguments, NowProvider], JsonObject]


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    task_id: str
    title: str
    board_slug: str | None = None
    db_path: str | None = None


_HANDOFF_LEDGER: list[HandoffRecord] = []
_HANDOFF_LEDGER_LIMIT = 10


def _record_handoff(record: HandoffRecord) -> None:
    _HANDOFF_LEDGER.append(record)
    del _HANDOFF_LEDGER[:-_HANDOFF_LEDGER_LIMIT]


def latest_handoff() -> HandoffRecord | None:
    return _HANDOFF_LEDGER[-1] if _HANDOFF_LEDGER else None


def clear_handoff_ledger() -> None:
    """Test helper: forget handoffs recorded in this process."""
    _HANDOFF_LEDGER.clear()


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


def _resolve_hermes_bin() -> str:
    configured = os.getenv("HERMES_KANBAN_BIN") or os.getenv("HERMES_BIN")
    if configured:
        return configured
    found = shutil.which("hermes")
    if found:
        return found
    candidates = (
        Path.home() / ".local" / "bin" / "hermes",
        Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "hermes"


def _run_hermes(cmd: list[str], timeout: float, failure: str) -> str:
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ExecutionContractError(f"Hermes executable not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExecutionContractError(f"{failure} timed out") from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        error = (
            completed.stderr
            or output
            or f"{failure} exited with status {completed.returncode}"
        ).strip()
        raise ExecutionContractError(error[-1_000:])
    return output


def _kanban_title(task: str) -> str:
    compact = " ".join(task.split())
    return compact[:80]


def _create_kanban_handoff(
    task: str,
    *,
    hermes_bin: str | None = None,
    board: tuple[str, str] | None = None,
) -> JsonObject:
    hermes_bin = hermes_bin or _resolve_hermes_bin()
    timeout = float(os.getenv("HERMES_KANBAN_CREATE_TIMEOUT_SECONDS", "30"))
    title = _kanban_title(task)
    body = (
        "Voice handoff from Okay Hermes Realtime.\n\n"
        "Acceptance criteria:\n"
        "- Handle the user's request with full Hermes Agent tools, memory, and skills.\n"
        "- Verify any concrete claims or file/system changes before completion.\n"
        "- Keep the final summary concise enough to read or speak back to the user.\n\n"
        f"Task:\n{task}"
    )
    cmd = [hermes_bin, "kanban"]
    if board is not None:
        cmd.extend(["--board", board[0]])
    cmd.extend([
        "create",
        title,
        "--body",
        body,
        "--assignee",
        os.getenv("HERMES_KANBAN_HEAVY_ASSIGNEE", "default"),
        "--created-by",
        "okay-hermes-realtime",
        "--max-runtime",
        os.getenv("HERMES_KANBAN_HEAVY_MAX_RUNTIME", "30m"),
        "--goal",
        "--json",
    ])
    provider = os.getenv("HERMES_KANBAN_HEAVY_PROVIDER", "").strip()
    model = os.getenv("HERMES_KANBAN_HEAVY_MODEL", "").strip()
    if model:
        cmd.extend(["--model", model])
        if provider:
            cmd.extend(["--provider", provider])
    output = _run_hermes(cmd, timeout, "Kanban create")
    created_task = _parse_kanban_create_output(output)
    if os.getenv("HERMES_KANBAN_DISPATCH_AFTER_CREATE", "1") not in {"0", "false", "False"}:
        _dispatch_kanban_once(hermes_bin, board[0] if board else None)
    return created_task


def _parse_kanban_create_output(output: str) -> JsonObject:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return {"raw_output": output, "task_id": output.split()[0] if output.split() else ""}
    if isinstance(parsed, dict):
        return parsed
    return {"raw_output": parsed}


def _dispatch_kanban_once(hermes_bin: str, board_slug: str | None = None) -> None:
    cmd = [hermes_bin, "kanban"]
    if board_slug:
        cmd.extend(["--board", board_slug])
    cmd.extend(["dispatch", "--max", "1"])
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(os.getenv("HERMES_KANBAN_DISPATCH_TIMEOUT_SECONDS", "15")),
        )


def _resolve_current_kanban_board(hermes_bin: str) -> tuple[str, str] | None:
    try:
        output = _run_hermes(
            [hermes_bin, "kanban", "boards", "list", "--json"],
            float(os.getenv("HERMES_KANBAN_BOARDS_TIMEOUT_SECONDS", "10")),
            "Kanban boards list",
        )
        boards = json.loads(output)
    except (ExecutionContractError, json.JSONDecodeError):
        return None
    if not isinstance(boards, list):
        return None
    for board in boards:
        if not isinstance(board, dict) or board.get("is_current") is not True:
            continue
        slug = board.get("slug")
        db_path = board.get("db_path")
        if isinstance(slug, str) and slug and isinstance(db_path, str) and db_path:
            return slug, db_path
    return None


def _show_kanban_task(task_id: str) -> JsonObject:
    hermes_bin = _resolve_hermes_bin()
    timeout = float(os.getenv("HERMES_KANBAN_SHOW_TIMEOUT_SECONDS", "15"))
    output = _run_hermes(
        [hermes_bin, "kanban", "show", task_id, "--json"],
        timeout,
        "Kanban show",
    )
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ExecutionContractError("Kanban show returned invalid JSON") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("task"), dict):
        raise ExecutionContractError("Kanban show returned an unexpected payload")
    return parsed


def _handoff_to_hermes_agent(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, HermesAgentArguments)
    hermes_bin = _resolve_hermes_bin()
    board = _resolve_current_kanban_board(hermes_bin)
    task = _create_kanban_handoff(
        arguments.task,
        hermes_bin=hermes_bin,
        board=board,
    )
    task_id = str(task.get("id") or task.get("task_id") or "")
    if task_id:
        _record_handoff(
            HandoffRecord(
                task_id=task_id,
                title=str(task.get("title", "")),
                board_slug=board[0] if board else None,
                db_path=board[1] if board else None,
            )
        )
    return {
        "action": "hermes.agent.handoff",
        "status": "queued",
        "task": task,
        "spoken_summary": "I added that to the Hermes Kanban board for the heavy agent.",
    }


def _check_heavy_agent_task(arguments: StrictArguments, _now_provider: NowProvider) -> JsonObject:
    assert isinstance(arguments, TaskStatusArguments)
    task_id = arguments.task_id
    if not task_id:
        latest = latest_handoff()
        if latest is None:
            raise ExecutionContractError("no heavy-agent task has been handed off yet")
        task_id = latest.task_id
    shown = _show_kanban_task(task_id)
    task = shown["task"]
    status = str(task.get("status", "unknown"))
    title = str(task.get("title", ""))
    summary = shown.get("latest_summary")
    spoken = _spoken_task_status(status, summary)
    return {
        "action": "hermes.agent.status",
        "task_id": task_id,
        "status": status,
        "title": title,
        "summary": summary,
        "spoken_summary": spoken,
    }


def _spoken_task_status(status: str, summary: str | None) -> str:
    if status == "done":
        detail = f" It says: {summary.strip()}" if summary and summary.strip() else ""
        return f"Your task is finished.{detail}"
    if status == "running":
        return "Your task is still in progress."
    if status in {"ready", "todo", "triage"}:
        return "Your task is queued and waiting for a worker."
    if status == "blocked":
        return "Your task is blocked."
    if status == "review":
        return "Your task is done and waiting for review."
    if status == "scheduled":
        return "Your task is scheduled for later."
    return f"Your task status is {status}."


def _permission_resolution_requires_live_session(
    _arguments: StrictArguments,
    _now_provider: NowProvider,
) -> JsonObject:
    raise ExecutionContractError(
        "permission resolution requires the originating live voice session"
    )


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
    CapabilityDefinition(
        name="handoff_to_heavy_agent",
        description=(
            "Delegate approved work to the full Hermes Agent. Call only for an explicit harmless "
            "Kanban/Hermes request, after confirmation of consequential explicit work, or after "
            "explicit consent to an offered handoff. The task must be a direct task containing "
            "the work itself and every user constraint, with no Hermes or Kanban routing language. "
            "Do not use for simple answers, direct lightweight tools, impossible, or unsafe work."
        ),
        arguments_model=HermesAgentArguments,
        execution="kanban",
        handler=_handoff_to_hermes_agent,
    ),
    CapabilityDefinition(
        name="check_heavy_agent_task",
        description=(
            "Report the status of a task previously handed to the heavy Hermes Agent. Call "
            "when the user asks what happened with their task; omit task_id to check the "
            "most recent handoff."
        ),
        arguments_model=TaskStatusArguments,
        execution="kanban",
        handler=_check_heavy_agent_task,
    ),
    CapabilityDefinition(
        name="resolve_heavy_agent_block",
        description=(
            "Record the user's explicit one-shot approval or denial for the exact blocked "
            "Kanban event that the assistant just described. Never infer approval, never use "
            "for another task or block event, and never broaden the requested permission."
        ),
        arguments_model=PermissionResolutionArguments,
        execution="kanban",
        handler=_permission_resolution_requires_live_session,
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


def parse_permission_resolution_arguments(
    arguments: str | Mapping[str, Any],
) -> PermissionResolutionArguments:
    parsed = _parse_arguments(arguments)
    try:
        return PermissionResolutionArguments.model_validate(parsed)
    except ValidationError as exc:
        raise ExecutionContractError(_format_validation_error(exc)) from exc


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
