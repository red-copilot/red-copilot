"""Experiment profiles for separating Agent, World, and heuristic planning."""
from __future__ import annotations

from enum import StrEnum


class SolverProfile(StrEnum):
    PI_ONLY = "pi-only"
    PI_WORLD = "pi-world"
    PI_WORLD_HEURISTIC = "pi-world-heuristic"
    PI_WORLD_GOAL = "pi-world-goal"


DEFAULT_SOLVER_PROFILE = SolverProfile.PI_WORLD_GOAL

# One table per profile: (world_context_enabled, candidate ranker id). ``None``
# means the profile runs without a heuristic planner (the Agent explores on its
# own), which is also what disables planner wiring at the call sites.
_PROFILE_SETTINGS: dict[SolverProfile, tuple[bool, str | None]] = {
    SolverProfile.PI_ONLY: (False, None),
    SolverProfile.PI_WORLD: (True, None),
    SolverProfile.PI_WORLD_HEURISTIC: (True, "heuristic"),
    SolverProfile.PI_WORLD_GOAL: (True, "goal-frontier"),
}


def solver_profile_settings(profile: SolverProfile | str) -> tuple[bool, bool]:
    """Return `(world_context_enabled, planner_enabled)` for a run profile."""
    world_context_enabled, candidate = _PROFILE_SETTINGS[_coerce_profile(profile)]
    return world_context_enabled, candidate is not None


def solver_profile_candidate(profile: SolverProfile | str) -> str | None:
    """Return the candidate ranker id for a profile ("heuristic"/"goal-frontier")."""
    return _PROFILE_SETTINGS[_coerce_profile(profile)][1]


def _coerce_profile(profile: SolverProfile | str) -> SolverProfile:
    try:
        return SolverProfile(profile)
    except ValueError as exc:
        choices = ", ".join(item.value for item in SolverProfile)
        raise ValueError(f"solver profile must be one of: {choices}") from exc
