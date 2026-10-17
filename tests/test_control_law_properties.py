"""Property-based falsification of the control law's safety envelope.

The hand-written cases in ``test_control_properties.py`` pin specific
transitions. These generated cases try to falsify the bounds themselves:
determinism, totality, that the hard limits always stop, that STOP is only ever
produced by a declared bound, and that invalid inputs fail closed.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from harness.runtime.checkpoint import RunCheckpoint
from harness.solver_policy import SolverAction, decide_solver_action

_EVENT_TYPES = st.sampled_from(
    [None, "tool.result", "tool.call", "progress.updated", "world.observe", "unrelated"]
)


@settings(max_examples=300, deadline=None)
@given(
    event_type=_EVENT_TYPES,
    no_progress_count=st.integers(min_value=0, max_value=500),
    unconfirmed_progress=st.integers(min_value=0, max_value=500),
    remaining_budget_fraction=st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
    ),
    max_no_progress=st.integers(min_value=1, max_value=64),
    max_unconfirmed_progress=st.integers(min_value=1, max_value=64),
)
def test_decision_is_deterministic_bounded_and_stops_only_at_a_bound(
    event_type: str | None,
    no_progress_count: int,
    unconfirmed_progress: int,
    remaining_budget_fraction: float,
    max_no_progress: int,
    max_unconfirmed_progress: int,
) -> None:
    kwargs = {
        "event_type": event_type,
        "no_progress_count": no_progress_count,
        "unconfirmed_progress": unconfirmed_progress,
        "remaining_budget_fraction": remaining_budget_fraction,
        "max_no_progress": max_no_progress,
        "max_unconfirmed_progress": max_unconfirmed_progress,
    }
    decision = decide_solver_action(**kwargs)

    # Determinism and totality.
    assert decision == decide_solver_action(**kwargs)
    assert decision.action in set(SolverAction)

    # Hard limits always stop, in the documented precedence order (the first
    # tripped bound wins), regardless of the other inputs.
    if no_progress_count >= max_no_progress:
        assert (decision.action, decision.reason) == (
            SolverAction.STOP,
            "no_progress_limit",
        )
    elif unconfirmed_progress >= max_unconfirmed_progress:
        assert (decision.action, decision.reason) == (
            SolverAction.STOP,
            "unconfirmed_progress_limit",
        )
    elif remaining_budget_fraction <= 0:
        assert (decision.action, decision.reason) == (
            SolverAction.STOP,
            "budget_exhausted",
        )
    elif decision.action is SolverAction.STOP:
        # No bound tripped, so the only legal stop is the low-budget guard.
        assert decision.reason == "low_budget_without_progress"
        assert no_progress_count > 0


@settings(max_examples=100, deadline=None)
@given(
    negative=st.integers(min_value=-1000, max_value=-1),
    non_positive_bound=st.integers(min_value=-1000, max_value=0),
    non_finite=st.sampled_from([float("nan"), float("inf"), float("-inf")]),
)
def test_invalid_control_inputs_fail_closed(
    negative: int, non_positive_bound: int, non_finite: float
) -> None:
    with pytest.raises(ValueError):
        decide_solver_action(no_progress_count=negative)
    with pytest.raises(ValueError):
        decide_solver_action(unconfirmed_progress=negative)
    with pytest.raises(ValueError):
        decide_solver_action(max_no_progress=non_positive_bound)
    with pytest.raises(ValueError):
        decide_solver_action(max_unconfirmed_progress=non_positive_bound)
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
