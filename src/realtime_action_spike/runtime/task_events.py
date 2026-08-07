from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

from .protocol import TaskEventKind, TaskEventMessage

logger = logging.getLogger(__name__)

VoicePublisher = Callable[[str, TaskEventMessage], Awaitable[bool]]
SessionPredicate = Callable[[str], bool]
PermissionDecision = Literal["approve_once", "deny"]
BlockKind = Literal["dependency", "needs_input", "transient", "capability"]
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|token|password|secret)\b(\s*[:=]\s*)(\S+)"
)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b"
)


@dataclass(frozen=True)
class KanbanTaskEvent:
    event_id: int
    task_id: str
    kind: str
    title: str
    status: str
    block_kind: str | None
    payload: dict[str, object]


@dataclass
class _TrackedTask:
    session_id: str
    task_id: str
    title: str
    board_slug: str
    db_path: Path
    cursor: int = 0
    terminal_seen: bool = False
    pending_voice: list[TaskEventMessage] = field(default_factory=list)


class DesktopNotifier(Protocol):
    async def notify(self, title: str, body: str, urgency: str) -> None: ...


class PermissionClient(Protocol):
    async def resolve(
        self,
        *,
        board_slug: str,
        task_id: str,
        block_event_id: int,
        decision: str,
        response: str,
    ) -> None: ...


class KanbanTaskEventSource:
    """Read exact task events from a Hermes board without migrating or writing it."""

    def read_events(
        self,
        db_path: str | Path,
        task_id: str,
        *,
        after_event_id: int,
    ) -> list[KanbanTaskEvent]:
        path = Path(db_path).expanduser().resolve()
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT e.id, e.task_id, e.kind, e.payload,
                       t.title, t.status, t.block_kind
                FROM task_events AS e
                JOIN tasks AS t ON t.id = e.task_id
                WHERE e.task_id = ? AND e.id > ?
                ORDER BY e.id ASC
                """,
                (task_id, after_event_id),
            ).fetchall()
        finally:
            connection.close()

        events: list[KanbanTaskEvent] = []
        for row in rows:
            payload = _parse_payload(row["payload"])
            events.append(
                KanbanTaskEvent(
                    event_id=int(row["id"]),
                    task_id=str(row["task_id"]),
                    kind=str(row["kind"]),
                    title=str(row["title"]),
                    status=str(row["status"]),
                    block_kind=str(row["block_kind"]) if row["block_kind"] else None,
                    payload=payload,
                )
            )
        return events

    def is_current_block(
        self,
        db_path: str | Path,
        task_id: str,
        block_event_id: int,
    ) -> bool:
        path = Path(db_path).expanduser().resolve()
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        try:
            task = connection.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            latest = connection.execute(
                """
                SELECT id, kind FROM task_events
                WHERE task_id = ? AND kind IN ('blocked', 'unblocked')
                ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        return bool(
            task
            and str(task[0]) == "blocked"
            and latest
            and int(latest[0]) == block_event_id
            and str(latest[1]) == "blocked"
        )


class NotifySendDesktopNotifier:
    """Use the freedesktop notification service, which Plasma renders natively."""

    def __init__(self, *, enabled: bool = True, binary: str | None = None) -> None:
        self._enabled = enabled
        self._binary = binary or shutil.which("notify-send")

    async def notify(self, title: str, body: str, urgency: str) -> None:
        if not self._enabled or not self._binary:
            return
        await asyncio.to_thread(self._notify_sync, title, body, urgency)

    def _notify_sync(self, title: str, body: str, urgency: str) -> None:
        assert self._binary is not None
        expires = "0" if urgency == "critical" else "8000"
        try:
            subprocess.run(
                [
                    self._binary,
                    "--app-name=Okay Hermes",
                    f"--urgency={urgency}",
                    f"--expire-time={expires}",
                    html.escape(title),
                    html.escape(body),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("KDE task notification failed", exc_info=True)


class HermesKanbanPermissionClient:
    """Record a one-shot voice decision, unblock, then nudge the dispatcher."""

    def __init__(self, hermes_binary: str | None = None) -> None:
        self._hermes_binary = hermes_binary or _resolve_hermes_binary()

    async def resolve(
        self,
        *,
        board_slug: str,
        task_id: str,
        block_event_id: int,
        decision: str,
        response: str,
    ) -> None:
        await asyncio.to_thread(
            self._resolve_sync,
            board_slug=board_slug,
            task_id=task_id,
            block_event_id=block_event_id,
            decision=decision,
            response=response,
        )

    def _resolve_sync(
        self,
        *,
        board_slug: str,
        task_id: str,
        block_event_id: int,
        decision: str,
        response: str,
    ) -> None:
        label = "APPROVED ONCE" if decision == "approve_once" else "DENIED"
        reason = f"Voice user decision for block event {block_event_id}: {label}. {response}"
        command = [
            self._hermes_binary,
            "kanban",
            "--board",
            board_slug,
            "unblock",
            task_id,
            "--reason",
            reason,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Hermes executable not found") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Hermes permission resolution timed out") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unblock failed").strip()
            raise RuntimeError(detail[:400])

        try:
            subprocess.run(
                [
                    self._hermes_binary,
                    "kanban",
                    "--board",
                    board_slug,
                    "dispatch",
                    "--max",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("post-unblock dispatch failed", exc_info=True)


class TaskEventCoordinator:
    """Bind Kanban task events to their originating live voice session."""

    def __init__(
        self,
        *,
        source: KanbanTaskEventSource,
        publish: VoicePublisher,
        session_is_active: SessionPredicate,
        notifier: DesktopNotifier,
        permission_client: PermissionClient,
        poll_interval_seconds: float = 1.0,
        start_background: bool = True,
    ) -> None:
        self._source = source
        self._publish = publish
        self._session_is_active = session_is_active
        self._notifier = notifier
        self._permission_client = permission_client
        self._poll_interval_seconds = poll_interval_seconds
        self._start_background = start_background
        self._tracked: dict[str, _TrackedTask] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._pending_blocks: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    def register_task(
        self,
        *,
        session_id: str,
        task_id: str,
        title: str,
        board_slug: str,
        db_path: str | Path,
    ) -> bool:
        if task_id in self._tracked:
            return False
        tracked = _TrackedTask(
            session_id=session_id,
            task_id=task_id,
            title=_bounded_text(title, 160) or task_id,
            board_slug=board_slug,
            db_path=Path(db_path).expanduser(),
        )
        self._tracked[task_id] = tracked
        if self._start_background:
            self._watchers[task_id] = asyncio.create_task(
                self._watch_task(task_id),
                name=f"kanban-task-events:{task_id}",
            )
        return True

    async def poll_once(self, task_id: str) -> None:
        tracked = self._tracked.get(task_id)
        if tracked is None:
            return
        events = await asyncio.to_thread(
            self._source.read_events,
            tracked.db_path,
            task_id,
            after_event_id=tracked.cursor,
        )
        for event in events:
            tracked.cursor = event.event_id
            if event.kind not in {kind.value for kind in TaskEventKind}:
                continue
            if event.kind == TaskEventKind.BLOCKED.value:
                is_current = await asyncio.to_thread(
                    self._source.is_current_block,
                    tracked.db_path,
                    task_id,
                    event.event_id,
                )
                if not is_current:
                    continue
            await self._handle_event(tracked, event)
            tracked.terminal_seen = (
                tracked.terminal_seen or event.kind == TaskEventKind.COMPLETED.value
            )

        await self._flush_voice(tracked)
        if tracked.terminal_seen and not tracked.pending_voice:
            self._tracked.pop(task_id, None)

    async def resolve_permission(
        self,
        *,
        session_id: str,
        task_id: str,
        block_event_id: int,
        decision: str,
        response: str,
    ) -> dict[str, object]:
        if decision not in {"approve_once", "deny"}:
            raise ValueError("decision must be approve_once or deny")
        normalized_response = _bounded_text(response, 500)
        if not normalized_response:
            raise ValueError("response must not be blank")

        async with self._lock:
            tracked = self._tracked.get(task_id)
            if tracked is None or tracked.session_id != session_id:
                raise ValueError("task is not owned by this voice session")
            if not self._session_is_active(session_id):
                raise ValueError("originating voice session is no longer live")
            pending_key = (session_id, task_id)
            pending = self._pending_blocks.get(pending_key)
            if pending is None:
                raise ValueError("permission request is no longer pending")
            if pending != block_event_id:
                raise ValueError("block event is stale")
            still_blocked = await asyncio.to_thread(
                self._source.is_current_block,
                tracked.db_path,
                task_id,
                block_event_id,
            )
            if not still_blocked:
                if self._pending_blocks.get(pending_key) == block_event_id:
                    self._pending_blocks.pop(pending_key, None)
                raise ValueError("permission request is no longer pending")
            if not self._session_is_active(session_id):
                raise ValueError("originating voice session is no longer live")
            await self._permission_client.resolve(
                board_slug=tracked.board_slug,
                task_id=task_id,
                block_event_id=block_event_id,
                decision=decision,
                response=normalized_response,
            )
            if self._pending_blocks.get(pending_key) == block_event_id:
                self._pending_blocks.pop(pending_key, None)

        return {
            "action": "hermes.agent.permission_resolution",
            "task_id": task_id,
            "block_event_id": block_event_id,
            "decision": decision,
            "status": "unblocked",
            "spoken_summary": (
                "I recorded your one-time approval and resumed the task."
                if decision == "approve_once"
                else "I recorded your denial and resumed the task so it can find another path."
            ),
        }

    async def shutdown(self) -> None:
        watchers = list(self._watchers.values())
        self._watchers.clear()
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        self._tracked.clear()
        self._pending_blocks.clear()

    async def _watch_task(self, task_id: str) -> None:
        try:
            while task_id in self._tracked:
                try:
                    await self.poll_once(task_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Kanban task watcher failed for %s", task_id, exc_info=True)
                if task_id in self._tracked:
                    await asyncio.sleep(self._poll_interval_seconds)
        finally:
            self._watchers.pop(task_id, None)

    async def _handle_event(
        self,
        tracked: _TrackedTask,
        event: KanbanTaskEvent,
    ) -> None:
        kind = TaskEventKind(event.kind)
        detail = _event_detail(kind, event.payload)
        title, body, urgency = _desktop_message(kind, event.title, detail)
        await self._notifier.notify(title, body, urgency)

        if kind not in {
            TaskEventKind.COMPLETED,
            TaskEventKind.BLOCKED,
            TaskEventKind.GAVE_UP,
        }:
            return

        block_kind = cast(
            BlockKind | None,
            event.block_kind
            if event.block_kind in {
                "dependency",
                "needs_input",
                "transient",
                "capability",
            }
            else None,
        )
        requires_user_input = kind is TaskEventKind.BLOCKED and block_kind != "dependency"
        message = TaskEventMessage(
            session_id=tracked.session_id,
            event_id=event.event_id,
            task_id=event.task_id,
            kind=kind,
            title=_bounded_text(event.title, 160) or tracked.title,
            detail=detail,
            requires_user_input=requires_user_input,
            block_kind=block_kind if kind is TaskEventKind.BLOCKED else None,
        )
        tracked.pending_voice.append(message)
        if requires_user_input:
            async with self._lock:
                self._pending_blocks[(tracked.session_id, tracked.task_id)] = event.event_id

    async def _flush_voice(self, tracked: _TrackedTask) -> None:
        if not self._session_is_active(tracked.session_id):
            tracked.pending_voice.clear()
            return
        while tracked.pending_voice:
            message = tracked.pending_voice[0]
            if not await self._publish(tracked.session_id, message):
                return
            tracked.pending_voice.pop(0)


def _parse_payload(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_detail(kind: TaskEventKind, payload: dict[str, object]) -> str | None:
    keys = {
        TaskEventKind.COMPLETED: ("summary", "result"),
        TaskEventKind.BLOCKED: ("reason",),
        TaskEventKind.GAVE_UP: ("error", "reason"),
        TaskEventKind.CRASHED: ("error",),
        TaskEventKind.TIMED_OUT: ("error",),
        TaskEventKind.BLOCK_LOOP_DETECTED: ("reason",),
    }[kind]
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            bounded = _bounded_text(value.splitlines()[0], 400)
            if bounded:
                return bounded
    if kind is TaskEventKind.TIMED_OUT and isinstance(payload.get("limit_seconds"), int):
        return f"Worker exceeded {payload['limit_seconds']} seconds and will retry."
    return None


def _desktop_message(
    kind: TaskEventKind,
    task_title: str,
    detail: str | None,
) -> tuple[str, str, str]:
    headings: dict[TaskEventKind, tuple[str, str]] = {
        TaskEventKind.COMPLETED: ("Okay Hermes · Task completed", "normal"),
        TaskEventKind.BLOCKED: ("Okay Hermes · Permission needed", "critical"),
        TaskEventKind.GAVE_UP: ("Okay Hermes · Task needs attention", "critical"),
        TaskEventKind.CRASHED: ("Okay Hermes · Worker will retry", "normal"),
        TaskEventKind.TIMED_OUT: ("Okay Hermes · Worker will retry", "normal"),
        TaskEventKind.BLOCK_LOOP_DETECTED: ("Okay Hermes · Task needs attention", "critical"),
    }
    title, urgency = headings[kind]
    body = _bounded_text(task_title, 160) or "Background task"
    if detail:
        body = f"{body}\n{detail}"
    return title, body, urgency


def _bounded_text(value: str, limit: int) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        str(value),
    )
    redacted = _SECRET_TOKEN_RE.sub("[REDACTED]", redacted)
    cleaned = "".join(
        " " if ord(character) < 32 else character
        for character in redacted
    )
    return " ".join(cleaned.split())[:limit]


def _resolve_hermes_binary() -> str:
    configured = os.getenv("HERMES_KANBAN_BIN") or os.getenv("HERMES_BIN")
    if configured:
        return str(Path(configured).expanduser())
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    return str(Path.home() / ".local" / "bin" / "hermes")
