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
    VERIFY = "verify"
    STOP = "stop"


@dataclass(frozen=True)
class SolverDecision:
    action: SolverAction
    reason: str


VERIFY_EVENTS = frozenset({"tool.result", "progress.updated", "world.observe"})
DEFAULT_MAX_NO_PROGRESS = 6
# Successful executions without confirmed progress are bounded separately and
# generously: they are neither progress nor a stall, so they must not trigger the
# hard no-progress stop. This is the safety net for a loop with no action-level
# verifier (for example a benchmark that only scores submitted flags).
DEFAULT_MAX_UNCONFIRMED_PROGRESS = 40
LOW_BUDGET_FRACTION = 0.05


def decide_solver_action(
    *,
    event_type: str | None = None,
    no_progress_count: int = 0,
    unconfirmed_progress: int = 0,
    remaining_budget_fraction: float = 1.0,
    max_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
    max_unconfirmed_progress: int = DEFAULT_MAX_UNCONFIRMED_PROGRESS,
) -> SolverDecision:
    """Choose the next loop action using persisted progress and hard bounds."""
    if no_progress_count < 0 or unconfirmed_progress < 0:
        raise ValueError("solver counters must be non-negative")
    if max_no_progress < 1 or max_unconfirmed_progress < 1:
        raise ValueError("solver bounds must be positive")
    if not math.isfinite(remaining_budget_fraction):
        raise ValueError("remaining budget fraction must be finite")

    if no_progress_count >= max_no_progress:
        return SolverDecision(SolverAction.STOP, "no_progress_limit")
    if unconfirmed_progress >= max_unconfirmed_progress:
        return SolverDecision(SolverAction.STOP, "unconfirmed_progress_limit")
    if remaining_budget_fraction <= 0:
        return SolverDecision(SolverAction.STOP, "budget_exhausted")
    if remaining_budget_fraction <= LOW_BUDGET_FRACTION and no_progress_count > 0:
        return SolverDecision(SolverAction.STOP, "low_budget_without_progress")
    if event_type in VERIFY_EVENTS:
        return SolverDecision(SolverAction.VERIFY, "verification_event")
    return SolverDecision(SolverAction.CONTINUE, "no_control_action_required")
