"""Offline replay of the run metrics the sweep review needed and could not get.

The fixtures are excerpts shaped like the real 2026-10-16 artifacts: a trace
with two tool results and two model usage records, and the delivered Agent
feedback stream. Every derived field must be reproducible from these files
alone - that is what makes ``harness report`` and the sweep summary comparable
without hand-written ``jq``.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.runtime.results import derive_run_metrics, steer_counts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run_metrics"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_steer_counts_group_delivered_feedback_by_bucket() -> None:
    records = _read_jsonl(FIXTURES / "agent_feedback_excerpt.jsonl")

    counts = steer_counts(records)

    assert counts == {
        "hint": 0,
        "finalize": 0,
        "submission": 0,
        "verification": 0,
    }


def test_legacy_unmapped_steers_are_ignored() -> None:
    """Old heuristic steer records do not inflate the new four-bucket metric."""
    records = _read_jsonl(FIXTURES / "agent_feedback_excerpt.jsonl")

    counts = steer_counts(records)
    mapped = sum(counts.values())

    assert mapped == 0
    assert sum(counts.values()) == 0


def test_derive_run_metrics_replays_trace_and_feedback() -> None:
    trace = _read_jsonl(FIXTURES / "trace_excerpt.jsonl")
    feedback = _read_jsonl(FIXTURES / "agent_feedback_excerpt.jsonl")

    metrics = derive_run_metrics(trace=trace, feedback=feedback)

    assert metrics["steer_counts"]["verification"] == 0
    assert metrics["tool_calls"] == 2
    assert metrics["tool_failures"] == 1
    assert metrics["tool_time_ms"] == 95000
    assert metrics["context_tokens_final"] == 24500
    assert metrics["slow_tool_calls"][0] == {
        "tool": "bash",
        "tool_call_id": "call-slow",
        "duration_ms": 90000,
        "is_error": False,
    }


def test_derive_run_metrics_is_empty_safe() -> None:
    metrics = derive_run_metrics()

    assert metrics["steer_counts"] == {
        "hint": 0,
        "finalize": 0,
        "submission": 0,
        "verification": 0,
    }
    assert metrics["tool_calls"] == 0
    assert metrics["tool_time_ms"] == 0
    assert metrics["context_tokens_final"] == 0
    assert metrics["slow_tool_calls"] == []


def test_slow_tool_calls_stay_bounded() -> None:
    trace = [
        {
            "type": "tool.result",
            "data": {
                "tool": "bash",
                "tool_call_id": f"call-{index}",
                "duration_ms": index * 1000,
                "is_error": False,
            },
        }
        for index in range(20)
    ]

    metrics = derive_run_metrics(trace=trace)

    assert metrics["tool_calls"] == 20
    assert len(metrics["slow_tool_calls"]) == 5
    assert [item["duration_ms"] for item in metrics["slow_tool_calls"]] == [
        19000,
        18000,
        17000,
        16000,
        15000,
    ]


def test_derive_run_metrics_ignores_malformed_records() -> None:
    trace = [
        {"type": "tool.result", "data": {"duration_ms": "oops"}},
        {"type": "tool.result", "data": None},
        {"type": "model.usage", "data": {"input_tokens": None, "cache_read_tokens": 5}},
        "not-a-mapping",
    ]

    metrics = derive_run_metrics(trace=trace)

    assert metrics["tool_calls"] == 1
    assert metrics["tool_time_ms"] == 0
    assert metrics["context_tokens_final"] == 5


def test_new_steer_buckets_are_exact() -> None:
    records = [
        {"type": "solver.hint"},
        {"type": "benchmark.feedback"},
        {"type": "solver.submission_due"},
        {"type": "solver.submission_reask"},
        {"type": "solver.closing_session"},
        {"type": "solver.finalize"},
    ]

    counts = steer_counts(records)

    assert counts["hint"] == 1
    assert counts["verification"] == 1
    assert counts["submission"] == 3
    assert counts["finalize"] == 1
