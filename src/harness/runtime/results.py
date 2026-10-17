"""Shared result finalization for all Harness execution entry points."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..progress import (
    SLOW_TOOL_CALLS,
    STEER_BUCKETS,
    empty_steer_counts,
)
from ..trace import TraceRecorder


def find_empty_run_dirs(runs_root: Path) -> list[Path]:
    """Run directories that hold no artifact at all.

    An aborted provision used to leave the run directory behind with nothing but
    empty subdirectories; the 2026-10-16 TSec sweep accumulated 51 of them.
    Anything real - a result, a trace, a checkpoint, an Agent note - protects a
    directory from this scan, so it only ever reports work that never happened.
    """
    empty: list[Path] = []
    root = Path(runs_root)
    if not root.is_dir():
        return empty
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            has_file = any(child.is_file() for child in entry.rglob("*"))
        except OSError:
            continue
        if not has_file:
            empty.append(entry)
    return empty


def steer_counts(records: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count delivered Agent observations by bucket.

    ``records`` is the Agent feedback stream (``agent.feedback.jsonl``) or the
    equivalent in-memory list. Unmapped observation kinds - handshakes,
    ``aci.feedback``, benchmark verdicts - are deliberately not steers and are
    not counted.
    """
    counts = empty_steer_counts()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        bucket = STEER_BUCKETS.get(record.get("type"))
        if bucket is not None:
            counts[bucket] += 1
    return counts


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def derive_run_metrics(
    *,
    trace: Iterable[Mapping[str, Any]] = (),
    feedback: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recompute the run metrics from the artifacts a finished run left behind.

    Everything here is derived from the trace and the delivered feedback, so a
    historic run can be re-measured without re-running it and the stored metrics
    can be checked against the record. See ``tests/test_run_metrics.py``.
    """
    tool_calls = 0
    tool_failures = 0
    tool_time_ms = 0
    context_tokens_final = 0
    slow: list[dict[str, Any]] = []
    for event in trace:
        if not isinstance(event, Mapping):
            continue
        data = event.get("data")
        if not isinstance(data, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "tool.result":
            tool_calls += 1
            duration = _int_or_none(data.get("duration_ms")) or 0
            failed = bool(data.get("is_error")) or (
                _int_or_none(data.get("exit_code")) not in (0, None)
            )
            if failed:
                tool_failures += 1
            tool_time_ms += duration
            slow.append(
                {
                    "tool": data.get("tool"),
                    "tool_call_id": data.get("tool_call_id"),
                    "duration_ms": duration,
                    "is_error": bool(data.get("is_error")),
                }
            )
        elif event_type == "model.usage":
            prompt = (_int_or_none(data.get("input_tokens")) or 0) + (
                _int_or_none(data.get("cache_read_tokens")) or 0
            )
            context_tokens_final = prompt
    slow.sort(key=lambda item: item["duration_ms"], reverse=True)
    return {
        "steer_counts": steer_counts(feedback),
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "tool_time_ms": tool_time_ms,
        "slow_tool_calls": slow[:SLOW_TOOL_CALLS],
        "context_tokens_final": context_tokens_final,
    }


def submission_fraction(first_submission_ms: int | None, duration_ms: int | None) -> float | None:
    """Where in the wall clock the first candidate was submitted.

    A run that submits at the deadline learns nothing from the platform verdict
    inside its own window; the 2026-10-16 successes submitted at 0.99.
    """
    if first_submission_ms is None or duration_ms is None or duration_ms <= 0:
        return None
    fraction = first_submission_ms / duration_ms
    return round(min(1.0, max(0.0, fraction)), 3)


def persist_run_result(*, run_dir: Path, trace: TraceRecorder, result: dict[str, Any]) -> None:
    """Write the public result schema and emit the terminal trace event.

    Callers own their domain-specific fields, verification and teardown.
    This function preserves the existing JSON encoding and event payload.
    """
    path = run_dir / "result.json"
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".result-", dir=run_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link publishes the fully written file atomically and fails if
            # another finalizer has already committed this run's terminal result.
            os.link(temporary, path, follow_symlinks=False)
            created = True
        except FileExistsError:
            created = False
            if not stat.S_ISREG(path.lstat().st_mode):
                raise RuntimeError("terminal result path is not a regular file")
            existing = path.read_bytes()
            if existing != payload:
                raise RuntimeError("terminal result already exists with different content")
        if created:
            directory_fd = os.open(run_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    if not created:
        return
    trace.emit(
        "run.finished",
        data={
            "status": result["status"],
            "success": result["success"],
            "score": result["score"],
            "metrics": result["metrics"],
        },
    )
