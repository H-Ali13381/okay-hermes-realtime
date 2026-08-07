"""Tests for the Kanban heavy-agent handoff and status capabilities."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from realtime_action_spike import capabilities
from realtime_action_spike.capabilities import (
    CapabilityBroker,
    ExecutionContractError,
    clear_handoff_ledger,
    latest_handoff,
    parse_permission_resolution_arguments,
)


class _FakeRun:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    """Capture every subprocess.run invocation and serve canned results."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.results: dict[str, _FakeRun] = {}
        self.default = _FakeRun(0, stdout="")

    def add(self, marker: str, run: _FakeRun) -> None:
        self.results[marker] = run

    def __call__(self, cmd: list[str], **_kwargs: Any) -> _FakeRun:
        self.calls.append(list(cmd))
        for marker, run in self.results.items():
            if marker in cmd:
                return run
        return self.default


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for variable in (
        "HERMES_KANBAN_BIN",
        "HERMES_BIN",
        "HERMES_KANBAN_CREATE_TIMEOUT_SECONDS",
        "HERMES_KANBAN_DISPATCH_TIMEOUT_SECONDS",
        "HERMES_KANBAN_SHOW_TIMEOUT_SECONDS",
        "HERMES_KANBAN_BOARDS_TIMEOUT_SECONDS",
        "HERMES_KANBAN_DISPATCH_AFTER_CREATE",
        "HERMES_KANBAN_HEAVY_ASSIGNEE",
        "HERMES_KANBAN_HEAVY_MAX_RUNTIME",
        "HERMES_KANBAN_HEAVY_MODEL",
        "HERMES_KANBAN_HEAVY_PROVIDER",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_BIN", "/usr/local/bin/fake-hermes")
    clear_handoff_ledger()
    yield monkeypatch
    clear_handoff_ledger()


def _patch_run(monkeypatch: pytest.MonkeyPatch, runner: _RecordingRun) -> _RecordingRun:
    monkeypatch.setattr(capabilities.subprocess, "run", runner)
    return runner


def _create_payload(task_id: str = "t_handoff01", title: str = "Voice handoff: probe") -> str:
    return json.dumps({"id": task_id, "title": title, "status": "ready"})


def _show_payload(
    task_id: str = "t_handoff01",
    status: str = "done",
    summary: str | None = "All done.",
) -> str:
    return json.dumps(
        {
            "task": {"id": task_id, "title": "Voice handoff: probe", "status": status},
            "latest_summary": summary,
        }
    )


def _boards_payload() -> str:
    return json.dumps(
        [
            {
                "slug": "default",
                "db_path": "/tmp/hermes-test-kanban.db",
                "is_current": True,
            }
        ]
    )


def test_handoff_creates_kanban_card_with_request_body_and_defaults(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRun()
    runner.add("create", _FakeRun(0, stdout=_create_payload()))
    _patch_run(isolated_env, runner)

    output = CapabilityBroker().execute(
        "handoff_to_heavy_agent",
        {"request": "Summarize the   weather for tomorrow"},
    )

    assert output["ok"] is True
    assert output["execution"] == "kanban"
    result = output["result"]
    assert result["status"] == "queued"
    assert result["task"]["id"] == "t_handoff01"
    assert result["spoken_summary"]

    create_cmd = next(call for call in runner.calls if "create" in call)
    assert create_cmd[:4] == [
        "/usr/local/bin/fake-hermes",
        "kanban",
        "create",
        "Voice handoff: Summarize the weather for tomorrow",
    ]
    body = create_cmd[create_cmd.index("--body") + 1]
    assert "Request:\nSummarize the   weather for tomorrow" in body
    assert "Acceptance criteria:" in body
    assert create_cmd[create_cmd.index("--assignee") + 1] == "default"
    assert create_cmd[create_cmd.index("--created-by") + 1] == "okay-hermes-realtime"
    assert create_cmd[create_cmd.index("--max-runtime") + 1] == "30m"
    assert "--goal" in create_cmd
    assert "--json" in create_cmd


def test_handoff_dispatches_once_after_create(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRun()
    runner.add("create", _FakeRun(0, stdout=_create_payload()))
    runner.add("dispatch", _FakeRun(1, stderr="dispatcher unavailable"))
    _patch_run(isolated_env, runner)

    output = CapabilityBroker().execute(
        "handoff_to_heavy_agent",
        {"request": "probe dispatch"},
    )

    assert output["ok"] is True
    dispatch_cmd = next(call for call in runner.calls if "dispatch" in call)
    assert dispatch_cmd == [
        "/usr/local/bin/fake-hermes",
        "kanban",
        "dispatch",
        "--max",
        "1",
    ]


def test_handoff_skips_dispatch_when_disabled(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    isolated_env.setenv("HERMES_KANBAN_DISPATCH_AFTER_CREATE", "0")
    runner = _RecordingRun()
    runner.add("create", _FakeRun(0, stdout=_create_payload()))
    _patch_run(isolated_env, runner)

    output = CapabilityBroker().execute(
        "handoff_to_heavy_agent",
        {"request": "probe no dispatch"},
    )

    assert output["ok"] is True
    assert all("dispatch" not in call for call in runner.calls)


def test_handoff_failure_from_kanban_is_controlled(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRun()
    runner.add("create", _FakeRun(2, stderr="board not initialized"))
    _patch_run(isolated_env, runner)

    with pytest.raises(ExecutionContractError, match="board not initialized"):
        CapabilityBroker().execute("handoff_to_heavy_agent", {"request": "probe failure"})


def test_handoff_create_timeout_is_controlled(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    def slow_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=0.001)

    isolated_env.setattr(capabilities.subprocess, "run", slow_run)

    with pytest.raises(ExecutionContractError, match="timed out"):
        CapabilityBroker().execute("handoff_to_heavy_agent", {"request": "probe timeout"})


def test_handoff_missing_hermes_binary_is_controlled_without_path(
    isolated_env: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """systemd-style environment: no PATH, HOME without hermes fallbacks."""
    home = tmp_path_factory.mktemp("empty-home")
    isolated_env.delenv("HERMES_KANBAN_BIN", raising=False)
    isolated_env.delenv("HERMES_BIN", raising=False)
    isolated_env.setenv("PATH", "")
    isolated_env.setenv("HOME", str(home))

    def missing_run(cmd: list[str], **_kwargs: Any) -> _FakeRun:
        raise FileNotFoundError(cmd[0])

    isolated_env.setattr(capabilities.subprocess, "run", missing_run)

    with pytest.raises(ExecutionContractError, match="Hermes executable not found"):
        CapabilityBroker().execute("handoff_to_heavy_agent", {"request": "probe binary"})


def test_handoff_records_ledger_and_status_reports_most_recent(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRun()
    runner.add("create", _FakeRun(0, stdout=_create_payload("t_recent42")))
    runner.add("boards", _FakeRun(0, stdout=_boards_payload()))
    runner.add("show", _FakeRun(0, stdout=_show_payload("t_recent42", "done", "It worked.")))
    _patch_run(isolated_env, runner)

    CapabilityBroker().execute("handoff_to_heavy_agent", {"request": "first task"})
    output = CapabilityBroker().execute("check_heavy_agent_task", {})

    show_cmd = runner.calls[-1]
    assert show_cmd[:4] == ["/usr/local/bin/fake-hermes", "kanban", "show", "t_recent42"]
    assert "--json" in show_cmd

    result = output["result"]
    assert result["task_id"] == "t_recent42"
    assert result["status"] == "done"
    assert result["summary"] == "It worked."
    assert result["spoken_summary"] == "Your task is finished. It says: It worked."
    handoff = latest_handoff()
    assert handoff is not None
    assert handoff.board_slug == "default"
    assert handoff.db_path == "/tmp/hermes-test-kanban.db"
    create_cmd = next(call for call in runner.calls if "create" in call)
    assert create_cmd[2:5] == ["--board", "default", "create"]
    dispatch_cmd = next(call for call in runner.calls if "dispatch" in call)
    assert dispatch_cmd[2:5] == ["--board", "default", "dispatch"]


def test_permission_resolution_arguments_are_exact_and_one_shot() -> None:
    parsed = parse_permission_resolution_arguments(
        {
            "task_id": "t_recent42",
            "block_event_id": 17,
            "decision": "approve_once",
            "response": "Yes, overwrite that one file.",
        }
    )

    assert parsed.task_id == "t_recent42"
    assert parsed.block_event_id == 17
    assert parsed.decision == "approve_once"

    with pytest.raises(ExecutionContractError, match="decision"):
        parse_permission_resolution_arguments(
            {
                "task_id": "t_recent42",
                "block_event_id": 17,
                "decision": "allow_always",
                "response": "yes",
            }
        )


def test_status_accepts_explicit_task_id(isolated_env: pytest.MonkeyPatch) -> None:
    runner = _RecordingRun()
    runner.add("show", _FakeRun(0, stdout=_show_payload("t_explicit1", "running", None)))
    _patch_run(isolated_env, runner)

    output = CapabilityBroker().execute(
        "check_heavy_agent_task",
        {"task_id": "t_explicit1"},
    )

    result = output["result"]
    assert result["status"] == "running"
    assert result["spoken_summary"] == "Your task is still in progress."


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ready", "Your task is queued and waiting for a worker."),
        ("todo", "Your task is queued and waiting for a worker."),
        ("blocked", "Your task is blocked."),
        ("review", "Your task is done and waiting for review."),
        ("scheduled", "Your task is scheduled for later."),
        ("triage", "Your task is queued and waiting for a worker."),
    ],
)
def test_status_spoken_summary_covers_board_states(
    isolated_env: pytest.MonkeyPatch, status: str, expected: str
) -> None:
    runner = _RecordingRun()
    runner.add("show", _FakeRun(0, stdout=_show_payload(status=status, summary=None)))
    _patch_run(isolated_env, runner)

    output = CapabilityBroker().execute(
        "check_heavy_agent_task",
        {"task_id": "t_state01"},
    )

    assert output["result"]["spoken_summary"] == expected


def test_status_without_any_handoff_is_controlled(
    isolated_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ExecutionContractError, match="no heavy-agent task"):
        CapabilityBroker().execute("check_heavy_agent_task", {})


def test_status_show_failure_is_controlled(isolated_env: pytest.MonkeyPatch) -> None:
    runner = _RecordingRun()
    runner.add("show", _FakeRun(1, stderr="unknown task id"))
    _patch_run(isolated_env, runner)

    with pytest.raises(ExecutionContractError, match="unknown task id"):
        CapabilityBroker().execute("check_heavy_agent_task", {"task_id": "t_missing"})
