"""Pure, bounded stop decisions for the interactive solver loop.

The loop no longer plans: the Agent owns cognition and the Harness only bounds
it. What remains is the safety law - when a run must stop or verify - with no
planner, no skill ranking and no replan counter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class SolverAction(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


@dataclass(frozen=True)
class SolverDecision:
    action: SolverAction
    reason: str


def decide_solver_action(
    *,
    remaining_budget_fraction: float = 1.0,
) -> SolverDecision:
    """Return only the externally bounded stop decision."""
    if not math.isfinite(remaining_budget_fraction):
        raise ValueError("remaining budget fraction must be finite")
    if remaining_budget_fraction <= 0:
        return SolverDecision(SolverAction.STOP, "budget_exhausted")
    return SolverDecision(SolverAction.CONTINUE, "no_control_action_required")
