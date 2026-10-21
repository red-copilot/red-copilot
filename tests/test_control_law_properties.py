"""Property-based falsification of the externally bounded control law.

The Harness stops only on an exhausted external budget. Deterministic counters
such as failures or repeated commands are observations, not control inputs.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from harness.runtime.checkpoint import RunCheckpoint
from harness.solver_policy import SolverAction, decide_solver_action


@settings(max_examples=500, deadline=None)
@given(
    remaining_budget_fraction=st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
    )
)
def test_decision_is_total_deterministic_and_stops_only_at_zero_budget(
    remaining_budget_fraction: float,
) -> None:
    decision = decide_solver_action(remaining_budget_fraction=remaining_budget_fraction)

    assert decision == decide_solver_action(
        remaining_budget_fraction=remaining_budget_fraction
    )
    assert decision.action in {SolverAction.CONTINUE, SolverAction.STOP}
    if remaining_budget_fraction <= 0:
        assert (decision.action, decision.reason) == (
            SolverAction.STOP,
            "budget_exhausted",
        )
    else:
        assert (decision.action, decision.reason) == (
            SolverAction.CONTINUE,
            "no_control_action_required",
        )


@settings(max_examples=100, deadline=None)
@given(non_finite=st.sampled_from([float("nan"), float("inf"), float("-inf")]))
def test_invalid_control_inputs_fail_closed(non_finite: float) -> None:
    with pytest.raises(ValueError):
        decide_solver_action(remaining_budget_fraction=non_finite)


# 8. Recovery-safety: checkpoint budget usage must survive restart without
# silently inflating prior use. Counters are integers; every metric is finite
# and non-negative, so a resumed run cannot under- or over-count its budget.
_COUNTER_FIELDS = ["input_tokens", "output_tokens", "total_tokens", "model_calls", "tool_calls"]


def _checkpoint(**budget: object) -> RunCheckpoint:
    return RunCheckpoint(
        run_id="run-property",
        world_revision=0,
        event_offset=0,
        feedback_offset=0,
        budget_used=budget,
    )


@given(
    field=st.sampled_from(_COUNTER_FIELDS),
    value=st.integers(min_value=0, max_value=10**9),
)
def test_checkpoint_accepts_non_negative_integer_counters(field: str, value: int) -> None:
    checkpoint = _checkpoint(**{field: value})
    assert checkpoint.budget_used[field] == value


@given(
    field=st.sampled_from(_COUNTER_FIELDS),
    malformed=st.one_of(
        st.booleans(),
        st.text(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(max_value=-1),
    ),
)
def test_checkpoint_rejects_malformed_counters(field: str, malformed: object) -> None:
    with pytest.raises(ValidationError):
        _checkpoint(**{field: malformed})


@given(
    value=st.one_of(
        st.integers(min_value=0),
        st.floats(min_value=0.0, allow_nan=False, allow_infinity=False),
    )
)
def test_checkpoint_accepts_non_negative_scalar_costs(value: object) -> None:
    checkpoint = _checkpoint(cost_usd=value)
    assert checkpoint.budget_used["cost_usd"] == value


@given(
    bad=st.one_of(
        st.booleans(),
        st.text(),
        st.sampled_from([float("nan"), float("inf"), float("-inf")]),
        st.integers(max_value=-1),
        st.floats(max_value=-1e-6, allow_nan=False),
    )
)
def test_checkpoint_rejects_malformed_scalar_costs(bad: object) -> None:
    with pytest.raises(ValidationError):
        _checkpoint(cost_usd=bad)
