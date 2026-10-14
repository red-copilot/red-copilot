"""Experiment profiles for separating Agent, World, and heuristic planning."""
from __future__ import annotations

from enum import StrEnum


class SolverProfile(StrEnum):
    PI_ONLY = "pi-only"
    PI_WORLD = "pi-world"
    PI_WORLD_HEURISTIC = "pi-world-heuristic"
    PI_WORLD_GOAL = "pi-world-goal"


DEFAULT_SOLVER_PROFILE = SolverProfile.PI_WORLD_GOAL

# Candidate ranker selected per profile. ``None`` means the profile runs without
# a heuristic planner (the Agent explores on its own).
_PROFILE_CANDIDATE: dict[SolverProfile, str | None] = {
    SolverProfile.PI_ONLY: None,
    SolverProfile.PI_WORLD: None,
    SolverProfile.PI_WORLD_HEURISTIC: "heuristic",
    SolverProfile.PI_WORLD_GOAL: "goal-frontier",
}


def solver_profile_settings(profile: SolverProfile | str) -> tuple[bool, bool]:
    """Return `(world_context_enabled, planner_enabled)` for a run profile."""
    selected = _coerce_profile(profile)
    return {
        SolverProfile.PI_ONLY: (False, False),
        SolverProfile.PI_WORLD: (True, False),
        SolverProfile.PI_WORLD_HEURISTIC: (True, True),
        SolverProfile.PI_WORLD_GOAL: (True, True),
    }[selected]


def solver_profile_candidate(profile: SolverProfile | str) -> str | None:
    """Return the candidate ranker id for a profile ("heuristic"/"goal-frontier")."""
    return _PROFILE_CANDIDATE[_coerce_profile(profile)]


def _coerce_profile(profile: SolverProfile | str) -> SolverProfile:
    try:
        return SolverProfile(profile)
    except ValueError as exc:
        choices = ", ".join(item.value for item in SolverProfile)
        raise ValueError(f"solver profile must be one of: {choices}") from exc
