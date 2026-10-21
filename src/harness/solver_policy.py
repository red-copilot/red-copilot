"""Pure, externally bounded stop decisions for the interactive solver loop.

The Harness owns environment, trust, accounting and recovery; the Agent owns
cognition. Control therefore stops only when an external budget is exhausted,
never because the Harness inferred that the Agent was repeating itself, making
no progress, or using a tool too slowly.
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


def decide_solver_action(*, remaining_budget_fraction: float = 1.0) -> SolverDecision:
    """Return the externally bounded stop decision."""
    if not math.isfinite(remaining_budget_fraction):
        raise ValueError("remaining budget fraction must be finite")
    if remaining_budget_fraction <= 0:
        return SolverDecision(SolverAction.STOP, "budget_exhausted")
    return SolverDecision(SolverAction.CONTINUE, "no_control_action_required")
