from __future__ import annotations

import json

import pytest

from harness.runtime.lifecycle import (
    RunLifecycle,
    RunPhase,
    RunStatus,
    resolve_run_phase,
    resolve_run_status,
    settle_with_trace,
    transition_with_trace,
)
from harness.trace import TraceRecorder


@pytest.mark.parametrize(
    ("status", "success", "expected"),
    [
        ("objective_completed", False, "succeeded"),
        ("objective_completed", True, "succeeded"),
        ("cancelled", True, "cancelled"),
        ("error", True, "failed"),
        ("timeout", True, "exhausted"),
        ("budget_exceeded", True, "exhausted"),
        ("finished", True, "succeeded"),
        ("finished", False, "failed"),
    ],
)
def test_resolve_run_phase_maps_status_and_verdict(
    status: RunStatus, success: bool, expected: RunPhase
) -> None:
    assert resolve_run_phase(status, success=success) == expected


@pytest.mark.parametrize(
    ("timed_out", "budget_exceeded", "objective_completed", "expected"),
    [
        (False, False, True, "objective_completed"),
        (True, True, True, "objective_completed"),
        (True, True, False, "timeout"),
        (False, True, False, "budget_exceeded"),
        (False, False, False, "finished"),
    ],
)
def test_resolve_run_status_precedence(
    timed_out: bool,
    budget_exceeded: bool,
    objective_completed: bool,
    expected: RunStatus,
) -> None:
    assert (
        resolve_run_status(
            timed_out=timed_out,
            budget_exceeded=budget_exceeded,
            objective_completed=objective_completed,
        )
        == expected
    )


def test_created_phase_cannot_skip_to_verifying() -> None:
    lifecycle = RunLifecycle()

    with pytest.raises(ValueError, match="invalid run transition"):
        lifecycle.transition("verifying")


def test_settle_for_status_uses_one_terminal_decision() -> None:
    lifecycle = RunLifecycle()
    lifecycle.transition("running")
    lifecycle.transition("verifying")

    assert lifecycle.settle_for_status("finished", success=True) == "succeeded"
    assert lifecycle.terminal

    with pytest.raises(ValueError, match="terminal run phase"):
        lifecycle.settle_for_status("error")


def test_harness_error_is_not_reported_as_exhaustion() -> None:
    lifecycle = RunLifecycle()
    lifecycle.transition("running")

    # The runner used to settle from separate booleans, so an evaluator error
    # could be reported as exhausted when the budget was also spent.
    assert lifecycle.settle_for_status("error", success=False) == "failed"


def test_settle_rejects_contradictory_outcome() -> None:
    with pytest.raises(ValueError, match="successful, exhausted"):
        RunLifecycle().settle(succeeded=True, exhausted=True)


def test_cancellation_wins_over_a_successful_verdict() -> None:
    lifecycle = RunLifecycle()
    lifecycle.transition("running")

    assert lifecycle.settle_for_status("cancelled", success=True) == "cancelled"


def test_transition_and_settle_helpers_emit_phase_events(tmp_path) -> None:
    lifecycle = RunLifecycle()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-helper", "task")

    assert transition_with_trace(lifecycle, trace, "running") == "running"
    assert transition_with_trace(lifecycle, trace, "verifying") == "verifying"
    assert settle_with_trace(lifecycle, trace, "finished", success=True) == "succeeded"

    phases = [
        event["data"]
        for event in (
            json.loads(line)
            for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        )
        if event["type"] == "run.phase"
    ]
    assert phases == [
        {"previous": "created", "phase": "running"},
        {"previous": "running", "phase": "verifying"},
        {"previous": "verifying", "phase": "succeeded", "status": "finished"},
    ]
