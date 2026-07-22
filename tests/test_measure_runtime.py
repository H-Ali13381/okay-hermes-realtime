from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.measure_runtime import (
    MeasurementError,
    ProcessSample,
    aggregate_component,
    compute_cpu_percent,
    compute_lifecycle_deltas,
    discover_process_tree,
    main,
    measure_runtime,
    parse_proc_stat,
    parse_smaps_rollup,
    read_process_sample,
    write_summary,
)


def proc_stat(
    pid: int,
    *,
    command: str = "worker",
    ppid: int = 1,
    user_ticks: int = 10,
    system_ticks: int = 5,
    start_ticks: int = 100,
) -> str:
    fields = [
        "S",
        str(ppid),
        "1",
        "1",
        "0",
        "-1",
        "4194304",
        "0",
        "0",
        "0",
        "0",
        str(user_ticks),
        str(system_ticks),
        "0",
        "0",
        "20",
        "0",
        "1",
        "0",
        str(start_ticks),
        "4096",
        "10",
    ]
    return f"{pid} ({command}) " + " ".join(fields) + "\n"


def write_process(
    proc_root: Path,
    pid: int,
    *,
    ppid: int = 1,
    rss_kib: int = 120,
    pss_kib: int = 80,
    user_ticks: int = 10,
    system_ticks: int = 5,
    command: str = "worker",
) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True)
    (process_dir / "stat").write_text(
        proc_stat(
            pid,
            command=command,
            ppid=ppid,
            user_ticks=user_ticks,
            system_ticks=system_ticks,
        ),
        encoding="utf-8",
    )
    (process_dir / "smaps_rollup").write_text(
        f"00400000-7fffffff ---p 00000000 00:00 0 [rollup]\n"
        f"Rss: {rss_kib} kB\n"
        f"Pss: {pss_kib} kB\n"
        "Shared_Clean: 4 kB\n",
        encoding="utf-8",
    )


def test_parse_smaps_rollup_requires_rss_and_pss() -> None:
    assert parse_smaps_rollup("Rss: 2048 kB\nPss: 1024 kB\n") == (2048, 1024)

    with pytest.raises(MeasurementError, match="Pss"):
        parse_smaps_rollup("Rss: 2048 kB\n")
    with pytest.raises(MeasurementError, match="unit"):
        parse_smaps_rollup("Rss: 2 MB\nPss: 1 MB\n")


def test_parse_proc_stat_handles_spaces_and_parentheses_in_command() -> None:
    parsed = parse_proc_stat(
        proc_stat(
            42,
            command="Brave Origin (Nightly)",
            ppid=9,
            user_ticks=123,
            system_ticks=45,
            start_ticks=777,
        )
    )

    assert parsed.pid == 42
    assert parsed.ppid == 9
    assert parsed.command == "Brave Origin (Nightly)"
    assert parsed.cpu_ticks == 168
    assert parsed.start_ticks == 777


def test_read_process_sample_reports_vanished_process(tmp_path: Path) -> None:
    with pytest.raises(MeasurementError, match="vanished"):
        read_process_sample(8123, proc_root=tmp_path)


def test_read_process_sample_does_not_turn_permission_error_into_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_process(tmp_path, 33)
    original_read_text = Path.read_text

    def denied(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "smaps_rollup":
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(MeasurementError, match="permission"):
        read_process_sample(33, proc_root=tmp_path)


def test_discover_process_tree_is_scoped_to_explicit_root(tmp_path: Path) -> None:
    write_process(tmp_path, 100, ppid=1)
    write_process(tmp_path, 101, ppid=100)
    write_process(tmp_path, 102, ppid=101)
    write_process(tmp_path, 999, ppid=1)

    assert discover_process_tree(100, proc_root=tmp_path) == (100, 101, 102)


def test_aggregate_component_sums_only_supplied_samples() -> None:
    samples = [
        ProcessSample(pid=10, ppid=1, start_ticks=1, cpu_ticks=30, rss_kib=100, pss_kib=70),
        ProcessSample(pid=11, ppid=10, start_ticks=2, cpu_ticks=15, rss_kib=60, pss_kib=40),
    ]

    aggregate = aggregate_component("brave", samples)

    assert aggregate == {
        "name": "brave",
        "pids": [10, 11],
        "rss_kib": 160,
        "pss_kib": 110,
        "cpu_ticks": 45,
        "complete": True,
        "errors": [],
    }


def test_aggregate_component_exposes_errors_instead_of_silent_zero() -> None:
    sample = ProcessSample(pid=10, ppid=1, start_ticks=1, cpu_ticks=30, rss_kib=100, pss_kib=70)

    aggregate = aggregate_component("controller", [sample], errors=["pid 11 vanished"])

    assert aggregate["rss_kib"] == 100
    assert aggregate["complete"] is False
    assert aggregate["errors"] == ["pid 11 vanished"]


def test_compute_cpu_percent_uses_matching_pid_and_start_time() -> None:
    before = {
        10: ProcessSample(pid=10, ppid=1, start_ticks=50, cpu_ticks=100, rss_kib=1, pss_kib=1),
        11: ProcessSample(pid=11, ppid=10, start_ticks=60, cpu_ticks=50, rss_kib=1, pss_kib=1),
    }
    after = {
        10: ProcessSample(pid=10, ppid=1, start_ticks=50, cpu_ticks=140, rss_kib=1, pss_kib=1),
        11: ProcessSample(pid=11, ppid=10, start_ticks=999, cpu_ticks=500, rss_kib=1, pss_kib=1),
    }

    assert compute_cpu_percent(before, after, elapsed_seconds=2.0, clock_ticks=100) == 20.0
    with pytest.raises(MeasurementError, match="elapsed"):
        compute_cpu_percent(before, after, elapsed_seconds=0, clock_ticks=100)


def test_compute_lifecycle_deltas_pairs_markers_within_same_session() -> None:
    events = [
        {"session_id": "old", "event": "page_process_started", "monotonic_ns": 1},
        {"session_id": "s1", "event": "wake_detected", "monotonic_ns": 1_000_000_000},
        {"session_id": "s1", "event": "page_process_started", "monotonic_ns": 1_125_000_000},
        {"session_id": "s1", "event": "page_started", "monotonic_ns": 2_000_000_000},
        {"session_id": "s1", "event": "webrtc_live", "monotonic_ns": 2_500_000_000},
        {"session_id": "s1", "event": "user_interruption", "monotonic_ns": 3_000_000_000},
        {
            "session_id": "s1",
            "event": "local_playback_suppressed",
            "monotonic_ns": 3_040_000_000,
        },
    ]

    deltas = compute_lifecycle_deltas(events)

    assert deltas["wake_detected_to_page_process_started_ms"] == 125.0
    assert deltas["page_started_to_webrtc_live_ms"] == 500.0
    assert deltas["user_interruption_to_local_playback_suppressed_ms"] == 40.0
    assert deltas["speech_stopped_to_first_model_audio_ms"] is None
    assert "missing marker" in deltas["errors"][0]


def test_compute_lifecycle_deltas_rejects_reversed_or_malformed_timestamps() -> None:
    with pytest.raises(MeasurementError, match="timestamp"):
        compute_lifecycle_deltas(
            [
                {"session_id": "s", "event": "wake_detected", "monotonic_ns": "now"},
            ]
        )


def test_write_summary_creates_machine_readable_json(tmp_path: Path) -> None:
    output = tmp_path / "artifacts" / "summary.json"
    summary = {
        "schema_version": 1,
        "components": {"controller": {"complete": True, "rss_kib": 100}},
        "combined": {"complete": True, "rss_kib": 100},
        "lifecycle": {"errors": []},
    }

    write_summary(output, summary)

    assert json.loads(output.read_text(encoding="utf-8")) == summary
    assert output.stat().st_mode & 0o777 == 0o600


def test_measure_runtime_reports_components_brave_tree_and_combined_total(tmp_path: Path) -> None:
    write_process(tmp_path, 10, rss_kib=100, pss_kib=70)
    write_process(tmp_path, 20, rss_kib=80, pss_kib=60)
    write_process(tmp_path, 30, rss_kib=40, pss_kib=30)
    write_process(tmp_path, 40, rss_kib=200, pss_kib=150)
    write_process(tmp_path, 41, ppid=40, rss_kib=50, pss_kib=35)

    summary = measure_runtime(
        {"controller": 10, "wake_listener": 20, "tray": 30, "brave": 40},
        proc_root=tmp_path,
        sample_seconds=0.001,
    )

    assert summary["components"]["controller"]["rss_kib"] == 100
    assert summary["components"]["brave"]["pids"] == [40, 41]
    assert summary["components"]["brave"]["pss_kib"] == 185
    assert summary["combined"]["rss_kib"] == 470
    assert summary["combined"]["pss_kib"] == 345
    assert summary["combined"]["complete"] is True


def test_measure_runtime_marks_missing_component_and_combined_total_incomplete(
    tmp_path: Path,
) -> None:
    write_process(tmp_path, 10)

    summary = measure_runtime(
        {"controller": 10, "wake_listener": 999},
        proc_root=tmp_path,
        sample_seconds=0.001,
    )

    assert summary["components"]["wake_listener"]["rss_kib"] is None
    assert summary["components"]["wake_listener"]["complete"] is False
    assert summary["combined"]["rss_kib"] is None
    assert summary["combined"]["complete"] is False
    assert "vanished" in summary["combined"]["errors"][0]


def test_cli_writes_summary_using_only_explicit_pid_roots(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    write_process(proc_root, 10, rss_kib=111, pss_kib=77)
    output = tmp_path / "artifacts" / "summary.json"

    exit_code = main(
        [
            "--controller-pid",
            "10",
            "--proc-root",
            str(proc_root),
            "--sample-seconds",
            "0.001",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert set(summary["components"]) == {"controller"}
    assert summary["components"]["controller"]["rss_kib"] == 111
