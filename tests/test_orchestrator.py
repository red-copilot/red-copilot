"""Resume budgeting for ``harness run --resume-run``."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harness.budget import UsageMetrics
from harness.models import BudgetSpec, ObjectiveSpec, TaskSpec
from harness.orchestrator import _remaining_budget_task


def _task(**budgets: object) -> TaskSpec:
    return TaskSpec(
        apiVersion="harness/v1",
        id="task-1",
        name="task-1",
        objective=ObjectiveSpec(description="find the flag"),
        budgets=BudgetSpec(**budgets),
    )


def test_resume_rebases_declared_usage_ceilings_on_what_is_left() -> None:
    """Adapters only see their own session usage, so they get the remainder."""
    task = _task(wall_time=3600, max_tokens=1000, max_tool_calls=40, max_cost_usd=2.0)
    started_at = datetime.now(UTC) - timedelta(seconds=100)
    used = UsageMetrics(total_tokens=400, tool_calls=10, cost_usd=0.5)

    resumed = _remaining_budget_task(task, started_at, used)

    assert task.budgets.wall_time - resumed.budgets.wall_time in {100, 101}
    assert resumed.budgets.max_tokens == 600
    assert resumed.budgets.max_tool_calls == 30
    assert resumed.budgets.max_cost_usd == pytest.approx(1.5)


def test_resume_leaves_undeclared_dimensions_unset() -> None:
    task = _task(wall_time=600, max_tokens=100)

    resumed = _remaining_budget_task(
        task, datetime.now(UTC), UsageMetrics(total_tokens=10)
    )

    assert resumed.budgets.max_model_calls is None
    assert resumed.budgets.max_tool_calls is None
    assert resumed.budgets.max_cost_usd is None


def test_resume_keeps_a_spent_usage_ceiling_armed_at_the_smallest_allowance() -> None:
    """A spent dimension opens the finalize window instead of refusing the resume."""
    task = _task(wall_time=600, max_tokens=100)

    resumed = _remaining_budget_task(
        task, datetime.now(UTC), UsageMetrics(total_tokens=500)
    )

    assert resumed.budgets.max_tokens == 1


def test_resume_refuses_only_on_a_spent_wall_clock() -> None:
    task = _task(wall_time=60, max_tokens=1)
    started_at = datetime.now(UTC) - timedelta(seconds=120)

    with pytest.raises(RuntimeError, match="wall_time"):
        _remaining_budget_task(task, started_at, UsageMetrics(total_tokens=10_000))
