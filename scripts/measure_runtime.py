#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MeasurementError(RuntimeError):
    """A runtime measurement could not be read or interpreted safely."""


@dataclass(frozen=True, slots=True)
class ParsedStat:
    pid: int
    command: str
    ppid: int
    cpu_ticks: int
    start_ticks: int


@dataclass(frozen=True, slots=True)
class ProcessSample:
    pid: int
    ppid: int
    start_ticks: int
    cpu_ticks: int
    rss_kib: int
    pss_kib: int


_LIFECYCLE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("wake_detected_to_page_process_started_ms", "wake_detected", "page_process_started"),
    ("page_started_to_webrtc_live_ms", "page_started", "webrtc_live"),
    ("speech_stopped_to_first_model_audio_ms", "speech_stopped", "first_model_audio"),
    (
        "user_interruption_to_local_playback_suppressed_ms",
        "user_interruption",
        "local_playback_suppressed",
    ),
    ("stop_or_close_to_microphone_tracks_stopped_ms", "stop_or_close", "microphone_tracks_stopped"),
    ("teardown_start_to_wake_rearmed_ms", "teardown_start", "wake_rearmed"),
)


def parse_smaps_rollup(text: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if not parts or parts[0] not in {"Rss:", "Pss:"}:
            continue
        if len(parts) != 3 or parts[2] != "kB":
            raise MeasurementError(f"{parts[0][:-1]} has unsupported unit")
        try:
            value = int(parts[1])
        except ValueError as exc:
            raise MeasurementError(f"{parts[0][:-1]} is not an integer") from exc
        if value < 0:
            raise MeasurementError(f"{parts[0][:-1]} is negative")
        values[parts[0][:-1]] = value

    missing = [name for name in ("Rss", "Pss") if name not in values]
    if missing:
        raise MeasurementError(f"smaps_rollup missing {', '.join(missing)}")
    return values["Rss"], values["Pss"]


def parse_proc_stat(text: str) -> ParsedStat:
    line = text.strip()
    open_paren = line.find("(")
    close_paren = line.rfind(")")
    if open_paren <= 0 or close_paren <= open_paren:
        raise MeasurementError("stat has malformed command field")
    try:
        pid = int(line[:open_paren].strip())
    except ValueError as exc:
        raise MeasurementError("stat has invalid pid") from exc
    command = line[open_paren + 1 : close_paren]
    fields = line[close_paren + 1 :].strip().split()
    if len(fields) < 20:
        raise MeasurementError("stat has too few fields")
    try:
        ppid = int(fields[1])
        user_ticks = int(fields[11])
        system_ticks = int(fields[12])
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise MeasurementError("stat has invalid numeric field") from exc
    if pid <= 0 or ppid < 0 or min(user_ticks, system_ticks, start_ticks) < 0:
        raise MeasurementError("stat contains out-of-range values")
    return ParsedStat(
        pid=pid,
        command=command,
        ppid=ppid,
        cpu_ticks=user_ticks + system_ticks,
        start_ticks=start_ticks,
    )


def _read_proc_text(path: Path, pid: int, kind: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MeasurementError(f"pid {pid} vanished while reading {kind}") from exc
    except PermissionError as exc:
        raise MeasurementError(f"permission denied reading {kind} for pid {pid}") from exc
    except OSError as exc:
        raise MeasurementError(f"could not read {kind} for pid {pid}") from exc


def read_process_sample(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessSample:
    if isinstance(pid, bool) or pid <= 0:
        raise MeasurementError("pid must be a positive integer")
    process_dir = proc_root / str(pid)
    stat = parse_proc_stat(_read_proc_text(process_dir / "stat", pid, "stat"))
    if stat.pid != pid:
        raise MeasurementError(f"stat pid mismatch for pid {pid}")
    rss_kib, pss_kib = parse_smaps_rollup(
        _read_proc_text(process_dir / "smaps_rollup", pid, "smaps_rollup")
    )
    return ProcessSample(
        pid=pid,
        ppid=stat.ppid,
        start_ticks=stat.start_ticks,
        cpu_ticks=stat.cpu_ticks,
        rss_kib=rss_kib,
        pss_kib=pss_kib,
    )


def discover_process_tree(root_pid: int, *, proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    if isinstance(root_pid, bool) or root_pid <= 0:
        raise MeasurementError("root pid must be a positive integer")
    parents: dict[int, int] = {}
    try:
        candidates = tuple(proc_root.iterdir())
    except PermissionError as exc:
        raise MeasurementError("permission denied listing proc root") from exc
    except OSError as exc:
        raise MeasurementError("could not list proc root") from exc

    for candidate in candidates:
        if not candidate.name.isdecimal():
            continue
        pid = int(candidate.name)
        try:
            stat = parse_proc_stat(_read_proc_text(candidate / "stat", pid, "stat"))
        except MeasurementError as exc:
            if "vanished" in str(exc):
                continue
            raise
        parents[stat.pid] = stat.ppid

    if root_pid not in parents:
        raise MeasurementError(f"pid {root_pid} vanished before process-tree discovery")
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if pid not in selected and parent in selected:
                selected.add(pid)
                changed = True
    return tuple(sorted(selected))


def aggregate_component(
    name: str,
    samples: Iterable[ProcessSample],
    *,
    errors: Sequence[str] = (),
) -> dict[str, Any]:
    collected = tuple(samples)
    error_list = list(errors)
    if collected:
        rss_kib: int | None = sum(sample.rss_kib for sample in collected)
        pss_kib: int | None = sum(sample.pss_kib for sample in collected)
        cpu_ticks: int | None = sum(sample.cpu_ticks for sample in collected)
    else:
        rss_kib = None
        pss_kib = None
        cpu_ticks = None
    return {
        "name": name,
        "pids": [sample.pid for sample in collected],
        "rss_kib": rss_kib,
        "pss_kib": pss_kib,
        "cpu_ticks": cpu_ticks,
        "complete": not error_list and bool(collected),
        "errors": error_list,
    }


def compute_cpu_percent(
    before: Mapping[int, ProcessSample],
    after: Mapping[int, ProcessSample],
    *,
    elapsed_seconds: float,
    clock_ticks: int,
) -> float:
    if not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        raise MeasurementError("elapsed time must be finite and positive")
    if clock_ticks <= 0:
        raise MeasurementError("clock tick rate must be positive")
    delta_ticks = 0
    for pid, later in after.items():
        earlier = before.get(pid)
        if earlier is None or earlier.start_ticks != later.start_ticks:
            continue
        if later.cpu_ticks < earlier.cpu_ticks:
            raise MeasurementError(f"cpu ticks moved backwards for pid {pid}")
        delta_ticks += later.cpu_ticks - earlier.cpu_ticks
    return round((delta_ticks / clock_ticks) / elapsed_seconds * 100.0, 3)


def _event_timestamp_ns(event: Mapping[str, Any]) -> int:
    value = event.get("monotonic_ns")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeasurementError("trace event has invalid monotonic timestamp")
    if not math.isfinite(float(value)) or value < 0:
        raise MeasurementError("trace event has invalid monotonic timestamp")
    return int(value)


def compute_lifecycle_deltas(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized: list[tuple[str, str, int]] = []
    for event in events:
        session_id = event.get("session_id")
        event_name = event.get("event", event.get("type"))
        if not isinstance(session_id, str) or not session_id:
            raise MeasurementError("trace event has invalid session_id")
        if not isinstance(event_name, str) or not event_name:
            raise MeasurementError("trace event has invalid event name")
        normalized.append((session_id, event_name, _event_timestamp_ns(event)))

    result: dict[str, Any] = {}
    errors: list[str] = []
    for metric, start_name, end_name in _LIFECYCLE_PAIRS:
        delta_ms: float | None = None
        sessions = dict.fromkeys(session_id for session_id, _, _ in normalized)
        for session_id in sessions:
            start_times = [
                timestamp
                for candidate_session, name, timestamp in normalized
                if candidate_session == session_id and name == start_name
            ]
            for start in start_times:
                ends = [
                    timestamp
                    for candidate_session, name, timestamp in normalized
                    if candidate_session == session_id and name == end_name and timestamp >= start
                ]
                if ends:
                    delta_ms = round((min(ends) - start) / 1_000_000, 3)
                    break
            if delta_ms is not None:
                break
        result[metric] = delta_ms
        if delta_ms is None:
            errors.append(f"missing marker pair: {start_name} -> {end_name}")
    result["errors"] = errors
    return result


def load_trace_jsonl(path: Path) -> list[Mapping[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MeasurementError("could not read lifecycle trace") from exc
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MeasurementError(f"trace line {line_number} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise MeasurementError(f"trace line {line_number} is not an object")
        events.append(value)
    return events


def _sample_pids(
    pids: Iterable[int], proc_root: Path
) -> tuple[dict[int, ProcessSample], list[str]]:
    samples: dict[int, ProcessSample] = {}
    errors: list[str] = []
    for pid in pids:
        try:
            samples[pid] = read_process_sample(pid, proc_root=proc_root)
        except MeasurementError as exc:
            errors.append(str(exc))
    return samples, errors


def measure_runtime(
    roots: Mapping[str, int],
    *,
    proc_root: Path = Path("/proc"),
    sample_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    trace_events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if not roots:
        raise MeasurementError("at least one tracked pid is required")
    if not math.isfinite(sample_seconds) or sample_seconds <= 0:
        raise MeasurementError("sample duration must be finite and positive")
    clock_ticks = os.sysconf("SC_CLK_TCK")

    initial_pids: dict[str, tuple[int, ...]] = {}
    discovery_errors: dict[str, list[str]] = {name: [] for name in roots}
    for name, root_pid in roots.items():
        try:
            initial_pids[name] = (
                discover_process_tree(root_pid, proc_root=proc_root)
                if name == "brave"
                else (root_pid,)
            )
        except MeasurementError as exc:
            initial_pids[name] = (root_pid,)
            discovery_errors[name].append(str(exc))

    before_by_component: dict[str, dict[int, ProcessSample]] = {}
    for name, pids in initial_pids.items():
        samples, errors = _sample_pids(pids, proc_root)
        before_by_component[name] = samples
        discovery_errors[name].extend(errors)

    started = time.monotonic()
    sleep(sample_seconds)
    elapsed = time.monotonic() - started

    components: dict[str, dict[str, Any]] = {}
    for name, root_pid in roots.items():
        final_pids = initial_pids[name]
        if name == "brave":
            try:
                final_pids = discover_process_tree(root_pid, proc_root=proc_root)
            except MeasurementError as exc:
                discovery_errors[name].append(str(exc))
        after, errors = _sample_pids(final_pids, proc_root)
        all_errors = [*discovery_errors[name], *errors]
        aggregate = aggregate_component(name, after.values(), errors=all_errors)
        try:
            aggregate["cpu_percent"] = compute_cpu_percent(
                before_by_component[name],
                after,
                elapsed_seconds=elapsed,
                clock_ticks=clock_ticks,
            )
        except MeasurementError as exc:
            aggregate["cpu_percent"] = None
            aggregate["complete"] = False
            aggregate["errors"].append(str(exc))
        components[name] = aggregate

    all_complete = all(component["complete"] for component in components.values())
    combined_errors = [
        f"{name}: {error}"
        for name, component in components.items()
        for error in component["errors"]
    ]
    combined = {
        "rss_kib": sum(component["rss_kib"] for component in components.values())
        if all(component["rss_kib"] is not None for component in components.values())
        else None,
        "pss_kib": sum(component["pss_kib"] for component in components.values())
        if all(component["pss_kib"] is not None for component in components.values())
        else None,
        "cpu_percent": round(
            sum(component["cpu_percent"] for component in components.values()), 3
        )
        if all(component["cpu_percent"] is not None for component in components.values())
        else None,
        "complete": all_complete,
        "errors": combined_errors,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "sample_seconds": round(elapsed, 6),
        "components": components,
        "combined": combined,
        "lifecycle": compute_lifecycle_deltas(trace_events) if trace_events else {"errors": []},
    }


def write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer pid") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("must be a positive pid")
    return pid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure replacement voice runtime RSS, PSS, CPU, and lifecycle latency."
    )
    parser.add_argument("--controller-pid", type=_positive_pid)
    parser.add_argument("--listener-pid", type=_positive_pid)
    parser.add_argument("--tray-pid", type=_positive_pid)
    parser.add_argument("--brave-pid", type=_positive_pid)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--sample-seconds", type=float, default=1.0)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    roots = {
        name: pid
        for name, pid in (
            ("controller", args.controller_pid),
            ("wake_listener", args.listener_pid),
            ("tray", args.tray_pid),
            ("brave", args.brave_pid),
        )
        if pid is not None
    }
    if not roots:
        parser.error("at least one component pid is required")
    try:
        events = load_trace_jsonl(args.trace) if args.trace else []
        summary = measure_runtime(
            roots,
            proc_root=args.proc_root,
            sample_seconds=args.sample_seconds,
            trace_events=events,
        )
        write_summary(args.output, summary)
    except MeasurementError as exc:
        parser.exit(1, f"measurement failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
