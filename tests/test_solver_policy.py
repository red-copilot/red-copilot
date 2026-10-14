from datetime import UTC, datetime, timedelta

import pytest

from harness.benchmark.base import (
    BENCHMARK_MAX_REPLANS,
    BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
)
from harness.progress import ProgressLedger
from harness.solver_policy import (
    SolverAction,
    decide_solver_action,
)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"event_type": "progress.updated"}, SolverAction.VERIFY),
        ({"event_type": "tool.call"}, SolverAction.CONTINUE),
        ({"event_type": "tool.result", "replan_reasons": ["world_state_changed"]}, SolverAction.REPLAN),
        ({"event_type": "progress.updated", "no_progress_count": 6}, SolverAction.STOP),
        ({"event_type": "tool.call", "replan_reasons": ["changed"], "replans_used": 8}, SolverAction.STOP),
        ({"event_type": "tool.call", "remaining_budget_fraction": 0.03, "no_progress_count": 2}, SolverAction.STOP),
        ({"event_type": "tool.call", "remaining_budget_fraction": 0}, SolverAction.STOP),
    ],
)
def test_solver_decisions_cover_continue_verify_replan_and_stop(kwargs, expected) -> None:
    decision = decide_solver_action(**kwargs)

    assert decision.action is expected
    assert decision.reason


def test_force_replan_is_explicit_and_bounded() -> None:
    assert decide_solver_action(force_replan=True).action is SolverAction.REPLAN
    stopped = decide_solver_action(force_replan=True, replans_used=8)
    assert stopped.action is SolverAction.STOP
    assert stopped.reason == "replan_limit"


def test_benchmark_stability_bounds_extend_long_horizon_exploration() -> None:
    mid_run = decide_solver_action(
        event_type="tool.result",
        unconfirmed_progress=80,
        replans_used=10,
        replan_reasons=["world_state_changed"],
        max_unconfirmed_progress=BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
        max_replans=BENCHMARK_MAX_REPLANS,
    )
    assert mid_run.action is SolverAction.REPLAN

    at_ceiling = decide_solver_action(
        event_type="tool.result",
        unconfirmed_progress=BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
        max_unconfirmed_progress=BENCHMARK_MAX_UNCONFIRMED_PROGRESS,
        max_replans=BENCHMARK_MAX_REPLANS,
    )
    assert at_ceiling.action is SolverAction.STOP
    assert at_ceiling.reason == "unconfirmed_progress_limit"


def test_wall_time_is_soft_so_the_solver_keeps_going_through_the_grace_window() -> None:
    ledger = ProgressLedger(budget_limits={"wall_time": 60}, wall_time_grace_seconds=180)
    ledger.started_at = datetime.now(UTC) - timedelta(seconds=90)

    # The soft deadline has passed, but the grace window keeps the budget
    # fraction positive so the solver does not hard-stop mid-finalize.
    assert ledger.wall_time_expired is True
    assert ledger.remaining_budget_fraction > 0.0

    ledger.started_at = datetime.now(UTC) - timedelta(seconds=60 + 180 + 1)
    assert ledger.remaining_budget_fraction == 0.0
