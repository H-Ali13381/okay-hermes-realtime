from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from realtime_action_spike.runtime.protocol import TaskEventKind, TaskEventMessage
from realtime_action_spike.runtime.task_events import (
    KanbanTaskEventSource,
    NotifySendDesktopNotifier,
    TaskEventCoordinator,
)


class RecordingPublisher:
    def __init__(self, active_session_id: str | None = "local-session-01") -> None:
        self.active_session_id = active_session_id
        self.messages: list[TaskEventMessage] = []

    def session_is_active(self, session_id: str) -> bool:
        return session_id == self.active_session_id

    async def publish(self, session_id: str, message: TaskEventMessage) -> bool:
        if not self.session_is_active(session_id):
            return False
        self.messages.append(message)
        return True


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def notify(self, title: str, body: str, urgency: str) -> None:
        self.messages.append((title, body, urgency))


class RecordingPermissionClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def resolve(
        self,
        *,
        board_slug: str,
        task_id: str,
        block_event_id: int,
        decision: str,
        response: str,
    ) -> None:
        self.calls.append(
            {
                "board_slug": board_slug,
                "task_id": task_id,
                "block_event_id": block_event_id,
                "decision": decision,
                "response": response,
            }
        )


def _create_board(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                block_kind TEXT
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            );
            INSERT INTO tasks(id, title, status, block_kind)
            VALUES ('t_voice01', 'Voice handoff: probe', 'running', NULL);
            """
        )


def _append_event(
    path: Path,
    kind: str,
    payload: dict[str, object] | None = None,
    *,
    status: str | None = None,
    block_kind: str | None = None,
) -> int:
    with sqlite3.connect(path) as conn:
        if status is not None:
            conn.execute(
                "UPDATE tasks SET status = ?, block_kind = ? WHERE id = 't_voice01'",
                (status, block_kind),
            )
        cursor = conn.execute(
            "INSERT INTO task_events(task_id, run_id, kind, payload, created_at) "
            "VALUES ('t_voice01', 7, ?, ?, 100)",
            (kind, json.dumps(payload) if payload is not None else None),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _coordinator(
    board: Path,
    *,
    publisher: RecordingPublisher | None = None,
    notifier: RecordingNotifier | None = None,
    permission_client: RecordingPermissionClient | None = None,
) -> tuple[
    TaskEventCoordinator,
    RecordingPublisher,
    RecordingNotifier,
    RecordingPermissionClient,
]:
    resolved_publisher = publisher or RecordingPublisher()
    resolved_notifier = notifier or RecordingNotifier()
    resolved_permissions = permission_client or RecordingPermissionClient()
    coordinator = TaskEventCoordinator(
        source=KanbanTaskEventSource(),
        publish=resolved_publisher.publish,
        session_is_active=resolved_publisher.session_is_active,
        notifier=resolved_notifier,
        permission_client=resolved_permissions,
        start_background=False,
    )
    coordinator.register_task(
        session_id="local-session-01",
        task_id="t_voice01",
        title="Voice handoff: probe",
        board_slug="default",
        db_path=board,
    )
    return coordinator, resolved_publisher, resolved_notifier, resolved_permissions


def test_read_only_source_returns_exact_task_events_after_cursor(tmp_path: Path) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    first_id = _append_event(board, "created", {"ignored": True})
    second_id = _append_event(
        board,
        "completed",
        {"summary": "All done.\nSecond line is not spoken."},
        status="done",
    )

    source = KanbanTaskEventSource()
    events = source.read_events(board, "t_voice01", after_event_id=first_id)

    assert [event.event_id for event in events] == [second_id]
    assert events[0].kind == "completed"
    assert events[0].title == "Voice handoff: probe"
    assert events[0].payload == {"summary": "All done.\nSecond line is not spoken."}


@pytest.mark.asyncio
async def test_completed_event_is_spoken_once_and_sent_to_kde(tmp_path: Path) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    event_id = _append_event(
        board,
        "completed",
        {"summary": "All done.\nPrivate detail stays out of the popup."},
        status="done",
    )
    coordinator, publisher, notifier, _permissions = _coordinator(board)

    await coordinator.poll_once("t_voice01")
    await coordinator.poll_once("t_voice01")

    assert publisher.messages == [
        TaskEventMessage(
            session_id="local-session-01",
            event_id=event_id,
            task_id="t_voice01",
            kind=TaskEventKind.COMPLETED,
            title="Voice handoff: probe",
            detail="All done.",
            requires_user_input=False,
            block_kind=None,
        )
    ]
    assert notifier.messages == [
        ("Okay Hermes · Task completed", "Voice handoff: probe\nAll done.", "normal")
    ]


@pytest.mark.asyncio
async def test_voice_delivery_retries_when_controller_queue_is_temporarily_full(
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    event_id = _append_event(
        board,
        "completed",
        {"summary": "All done."},
        status="done",
    )

    class RetryPublisher(RecordingPublisher):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def publish(self, session_id: str, message: TaskEventMessage) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                return False
            return await super().publish(session_id, message)

    retry_publisher = RetryPublisher()
    coordinator, publisher, _notifier, _permissions = _coordinator(
        board,
        publisher=retry_publisher,
    )

    await coordinator.poll_once("t_voice01")
    assert publisher.messages == []
    await coordinator.poll_once("t_voice01")

    assert retry_publisher.attempts == 2
    assert [message.event_id for message in publisher.messages] == [event_id]


@pytest.mark.asyncio
async def test_blocked_event_prompts_voice_and_authorizes_only_exact_block(tmp_path: Path) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    event_id = _append_event(
        board,
        "blocked",
        {"reason": "May I overwrite config.toml?"},
        status="blocked",
        block_kind="needs_input",
    )
    coordinator, publisher, notifier, permissions = _coordinator(board)

    await coordinator.poll_once("t_voice01")
    result = await coordinator.resolve_permission(
        session_id="local-session-01",
        task_id="t_voice01",
        block_event_id=event_id,
        decision="approve_once",
        response="Yes, overwrite that one file.",
    )

    assert publisher.messages[0].kind is TaskEventKind.BLOCKED
    assert publisher.messages[0].requires_user_input is True
    assert publisher.messages[0].block_kind == "needs_input"
    assert notifier.messages == [
        (
            "Okay Hermes · Permission needed",
            "Voice handoff: probe\nMay I overwrite config.toml?",
            "critical",
        )
    ]
    assert permissions.calls == [
        {
            "board_slug": "default",
            "task_id": "t_voice01",
            "block_event_id": event_id,
            "decision": "approve_once",
            "response": "Yes, overwrite that one file.",
        }
    ]
    assert result["status"] == "unblocked"
    assert result["decision"] == "approve_once"

    with pytest.raises(ValueError, match="no longer pending"):
        await coordinator.resolve_permission(
            session_id="local-session-01",
            task_id="t_voice01",
            block_event_id=event_id,
            decision="approve_once",
            response="replay",
        )


@pytest.mark.asyncio
async def test_stale_block_is_not_announced_when_task_already_resumed_and_completed(
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    _append_event(
        board,
        "blocked",
        {"reason": "May I overwrite config.toml?", "kind": "capability"},
        status="blocked",
        block_kind="capability",
    )
    _append_event(board, "unblocked", {"status": "ready"}, status="ready")
    completed_id = _append_event(
        board,
        "completed",
        {"summary": "Finished without the overwrite."},
        status="done",
    )
    coordinator, publisher, notifier, permissions = _coordinator(board)

    await coordinator.poll_once("t_voice01")

    assert [message.event_id for message in publisher.messages] == [completed_id]
    assert notifier.messages == [
        (
            "Okay Hermes · Task completed",
            "Voice handoff: probe\nFinished without the overwrite.",
            "normal",
        )
    ]
    assert permissions.calls == []


@pytest.mark.asyncio
async def test_permission_resolution_rejects_stale_session_event_and_ambiguous_decision(
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    event_id = _append_event(
        board,
        "blocked",
        {"reason": "Need permission"},
        status="blocked",
        block_kind="capability",
    )
    coordinator, publisher, _notifier, permissions = _coordinator(board)
    await coordinator.poll_once("t_voice01")

    with pytest.raises(ValueError, match="session"):
        await coordinator.resolve_permission(
            session_id="other-session-01",
            task_id="t_voice01",
            block_event_id=event_id,
            decision="approve_once",
            response="yes",
        )
    with pytest.raises(ValueError, match="block event"):
        await coordinator.resolve_permission(
            session_id="local-session-01",
            task_id="t_voice01",
            block_event_id=event_id + 1,
            decision="approve_once",
            response="yes",
        )
    with pytest.raises(ValueError, match="decision"):
        await coordinator.resolve_permission(
            session_id="local-session-01",
            task_id="t_voice01",
            block_event_id=event_id,
            decision="allow_always",
            response="yes",
        )

    publisher.active_session_id = None
    with pytest.raises(ValueError, match="live"):
        await coordinator.resolve_permission(
            session_id="local-session-01",
            task_id="t_voice01",
            block_event_id=event_id,
            decision="approve_once",
            response="yes",
        )

    assert permissions.calls == []


@pytest.mark.asyncio
async def test_stale_resolution_does_not_delete_a_newer_pending_block(tmp_path: Path) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    event_id = _append_event(
        board,
        "blocked",
        {"reason": "Need permission"},
        status="blocked",
        block_kind="capability",
    )
    coordinator, _publisher, _notifier, _permissions = _coordinator(board)
    await coordinator.poll_once("t_voice01")
    pending_blocks: dict[tuple[str, str], int] = coordinator._pending_blocks

    class ReplacedBlockSource:
        def read_events(self, *args: object, **kwargs: object) -> list[object]:
            return []

        def is_current_block(self, *args: object, **kwargs: object) -> bool:
            pending_blocks[("local-session-01", "t_voice01")] = event_id + 1
            return False

    coordinator._source = ReplacedBlockSource()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="no longer pending"):
        await coordinator.resolve_permission(
            session_id="local-session-01",
            task_id="t_voice01",
            block_event_id=event_id,
            decision="approve_once",
            response="yes",
        )

    assert pending_blocks[("local-session-01", "t_voice01")] == event_id + 1


@pytest.mark.asyncio
async def test_transient_retry_is_kde_only_and_stale_session_never_gets_voice(
    tmp_path: Path,
) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    _append_event(board, "crashed", {"error": "worker exited"}, status="ready")
    publisher = RecordingPublisher(active_session_id=None)
    coordinator, publisher, notifier, _permissions = _coordinator(board, publisher=publisher)

    await coordinator.poll_once("t_voice01")

    assert publisher.messages == []
    assert notifier.messages == [
        (
            "Okay Hermes · Worker will retry",
            "Voice handoff: probe\nworker exited",
            "normal",
        )
    ]


@pytest.mark.asyncio
async def test_spoken_and_desktop_details_redact_common_secret_shapes(tmp_path: Path) -> None:
    board = tmp_path / "kanban.db"
    _create_board(board)
    _append_event(
        board,
        "completed",
        {"summary": "Finished with API_KEY=sk-proj-1234567890abcdef"},
        status="done",
    )
    coordinator, publisher, notifier, _permissions = _coordinator(board)

    await coordinator.poll_once("t_voice01")

    assert publisher.messages[0].detail == "Finished with API_KEY=[REDACTED]"
    assert notifier.messages[0][1].endswith("API_KEY=[REDACTED]")


@pytest.mark.asyncio
async def test_desktop_notifier_uses_native_notify_send_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(
        "realtime_action_spike.runtime.task_events.subprocess.run",
        fake_run,
    )
    notifier = NotifySendDesktopNotifier(binary="/usr/bin/notify-send")

    await notifier.notify("Task <blocked>", "Need <permission>", "critical")

    assert calls == [
        (
            [
                "/usr/bin/notify-send",
                "--app-name=Okay Hermes",
                "--urgency=critical",
                "--expire-time=0",
                "Task &lt;blocked&gt;",
                "Need &lt;permission&gt;",
            ],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 2.0,
            },
        )
    ]
