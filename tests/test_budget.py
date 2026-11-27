import time
from pathlib import Path

import pytest

from harness.budget import (
    FINALIZE_GRACE_SECONDS,
    UsageCeiling,
    UsageMetrics,
    charge_usage_event,
    enforced_budget_limits,
    exceeded_usage_dimension,
    usage_budget_exceeded,
)
from harness.models import BudgetSpec
from harness.trace import TraceRecorder


def test_enforced_budget_limits_include_declared_dimensions() -> None:
    limits = enforced_budget_limits(
        BudgetSpec(wall_time=60, max_tokens=100, max_cost_usd=0.5)
    )
    assert limits == {"wall_time": 60.0, "max_tokens": 100.0, "max_cost_usd": 0.5}
    # Unset dimensions stay absent; nothing is silently dropped anymore.
    assert enforced_budget_limits(BudgetSpec(wall_time=30)) == {"wall_time": 30.0}


def test_usage_budget_exceeded_reports_first_crossed_dimension() -> None:
    metrics = UsageMetrics(total_tokens=120, model_calls=2)
    assert (
        usage_budget_exceeded(metrics, BudgetSpec(wall_time=60, max_tokens=100))
        == "max_tokens"
    )
    assert usage_budget_exceeded(metrics, BudgetSpec(wall_time=60, max_model_calls=5)) is None
    assert usage_budget_exceeded(UsageMetrics(), BudgetSpec(wall_time=60)) is None
    # At-limit counts as exceeded; cost compares as a float.
    assert (
        usage_budget_exceeded(
            UsageMetrics(model_calls=5), BudgetSpec(wall_time=60, max_model_calls=5)
        )
        == "max_model_calls"
    )
    assert (
        usage_budget_exceeded(
            UsageMetrics(cost_usd=0.5), BudgetSpec(wall_time=60, max_cost_usd=0.5)
        )
        == "max_cost_usd"
    )


def test_exceeded_usage_dimension_reads_ledger_keyed_usage() -> None:
    """The ledger-side scan mirrors the metrics-side scan on the shared table."""
    limits = enforced_budget_limits(BudgetSpec(wall_time=60, max_tokens=100))
    assert exceeded_usage_dimension({"tokens": 120.0}, limits) == "max_tokens"
    assert exceeded_usage_dimension({"tokens": 99.0}, limits) is None
    # A dimension never charged stays absent and never fires.
    assert exceeded_usage_dimension({}, limits) is None


def test_charge_usage_event_accounts_a_valid_model_usage() -> None:
    metrics = UsageMetrics()
    accepted = charge_usage_event(
        metrics,
        "model.usage",
        {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12, "model_calls": 1, "cost_usd": 0.2},
    )
    assert accepted is True
    assert metrics.total_tokens == 12
    assert metrics.model_calls == 1
    assert metrics.cost_usd == pytest.approx(0.2)


def test_charge_usage_event_fails_closed_on_malformed_or_negative_usage() -> None:
    metrics = UsageMetrics()
    # A usage event without model_calls is legal: the tokens still charge
    # and the call counter stays untouched (the Pi session path relies on
    # this for provider events that omit the call count).
    assert charge_usage_event(
        metrics, "model.usage", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    ) is True
    assert metrics.total_tokens == 2
    assert metrics.model_calls == 0
    metrics = UsageMetrics()
    cases = (
        # Negative counters.
        {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0, "model_calls": 1},
        # total below the input+output sum.
        {"input_tokens": 6, "output_tokens": 5, "total_tokens": 1, "model_calls": 1},
        # Non-integer counters.
        {"input_tokens": "8", "output_tokens": 4, "total_tokens": 12, "model_calls": 1},
        # Boolean counter.
        {"input_tokens": True, "output_tokens": 1, "total_tokens": 2, "model_calls": 1},
        # Non-finite or negative cost.
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "model_calls": 1, "cost_usd": -0.5},
    )
    for data in cases:
        assert charge_usage_event(metrics, "model.usage", data) is False, data
    assert metrics.total_tokens == 0
    assert metrics.model_calls == 0

    # Missing usage telemetry never charges anything.
    assert charge_usage_event(metrics, "model.usage_missing", {"source": "pi"}) is False

    # Malformed tool and model request counters fail closed too.
    assert charge_usage_event(metrics, "tool.call", {"count": -5}) is False
    assert charge_usage_event(metrics, "tool.call", {"count": "many"}) is False
    assert charge_usage_event(metrics, "model.request", {"count": "many"}) is False
    assert metrics.tool_calls == 0


def test_usage_ceiling_crossing_is_soft_with_a_grace_window(tmp_path: Path) -> None:
    """Crossing a usage ceiling opens the shared finalize grace window.

    The crossing is traced once and does not stop the run: only the window's
    edge (the same edge the adapters kill on) ends the session. This is the
    semantics the Pi RPC session's ``_account`` path drives through this
    class; the test exercises it directly at the unit level.
    """
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-ceiling", "task-ceiling")
    ceiling = UsageCeiling()
    budgets = BudgetSpec(wall_time=60, max_tokens=10)

    ceiling.observe(UsageMetrics(total_tokens=9), budgets, trace)
    assert ceiling.dimension is None
    assert ceiling.elapsed() is False

    ceiling.observe(
        UsageMetrics(total_tokens=12, model_calls=1, cost_usd=0.0), budgets, trace
    )
    assert ceiling.dimension == "max_tokens"
    assert ceiling.remaining() is not None and ceiling.remaining() > 0

    def crossing_marks() -> int:
        text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        return sum(1 for line in text.splitlines() if '"budget.exceeded"' in line)

    assert crossing_marks() == 1
    # Repeated observations cannot re-arm or re-trace the crossing.
    ceiling.observe(UsageMetrics(total_tokens=50), budgets, trace)
    assert crossing_marks() == 1

    # Time-travel past the window: the edge reports elapsed exactly once.
    assert ceiling.deadline is not None
    ceiling.deadline -= FINALIZE_GRACE_SECONDS + 1
    assert ceiling.elapsed() is True


def test_usage_ceiling_ignores_undeclared_dimensions(tmp_path: Path) -> None:
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-ceiling", "task-ceiling")
    ceiling = UsageCeiling()
    # Only the wall clock is declared; token and call growth never arms it.
    ceiling.observe(
        UsageMetrics(total_tokens=10**9, model_calls=10**6),
        BudgetSpec(wall_time=60),
        trace,
    )
    assert ceiling.dimension is None
    assert not (tmp_path / "trace.jsonl").exists() or (
        '"budget.exceeded"' not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    )


def test_usage_ceiling_remaining_is_none_before_armed(tmp_path: Path) -> None:
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-ceiling", "task-ceiling")
    ceiling = UsageCeiling()
    assert ceiling.remaining() is None
    assert ceiling.elapsed() is False
    ceiling.observe(UsageMetrics(), BudgetSpec(wall_time=1), trace)
    assert ceiling.dimension is None
    assert ceiling.remaining() is None
    # A real crossing arms a deadline slightly in the future.
    ceiling.observe(
        UsageMetrics(model_calls=3), BudgetSpec(wall_time=1, max_model_calls=2), trace
    )
    assert ceiling.dimension == "max_model_calls"
    assert ceiling.deadline is not None and ceiling.deadline > time.monotonic()
