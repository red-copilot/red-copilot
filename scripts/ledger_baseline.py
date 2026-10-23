#!/usr/bin/env python3
"""Measure append, verification and projection cost for synthetic ledgers."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from harness.ledger import LedgerStore, project_records


def _parse_events(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("events must be an integer") from exc
    if count <= 0:
        raise argparse.ArgumentTypeError("events must be positive")
    return count


def _synthetic_event(index: int) -> dict[str, object]:
    call_id = f"call-{index // 2}"
    if index % 2 == 0:
        return {
            "event_id": f"evt-{index}",
            "run_id": "run-ledger-baseline",
            "type": "tool.call",
            "ts": "2026-10-23T12:00:00+00:00",
            "actor": "agent:baseline",
            "data": {"tool": "bash", "tool_call_id": call_id},
        }
    return {
        "event_id": f"evt-{index}",
        "run_id": "run-ledger-baseline",
        "type": "tool.result",
        "ts": "2026-10-23T12:00:01+00:00",
        "actor": "agent:baseline",
        "data": {
            "tool": "bash",
            "tool_call_id": call_id,
            "execution_status": "succeeded",
            "is_error": False,
        },
    }


def run_baseline(event_count: int, work_dir: Path | None) -> dict[str, object]:
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="ledger-baseline-") as directory:
            return _run_baseline(event_count, Path(directory) / "ledger.db")
    work_dir.mkdir(parents=True, exist_ok=True)
    return _run_baseline(event_count, work_dir / "ledger.db")


def _run_baseline(event_count: int, path: Path) -> dict[str, object]:
    store = LedgerStore(path)
    start = time.perf_counter()
    for index in range(event_count):
        store.append_trace_event(_synthetic_event(index))
    append_seconds = time.perf_counter() - start

    start = time.perf_counter()
    store.verify()
    verify_seconds = time.perf_counter() - start

    start = time.perf_counter()
    projection = project_records(store.records())
    project_seconds = time.perf_counter() - start
    store.close()

    return {
        "event_count": event_count,
        "events_per_second": round(event_count / append_seconds, 3) if append_seconds else None,
        "append_ms": round(append_seconds * 1000, 3),
        "verify_ms": round(verify_seconds * 1000, 3),
        "project_ms": round(project_seconds * 1000, 3),
        "ledger_bytes": path.stat().st_size,
        "projected_actions": len(projection.actions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=_parse_events, default=10_000)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()
    print(json.dumps(run_baseline(args.events, args.work_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
