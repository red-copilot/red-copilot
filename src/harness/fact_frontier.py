"""Goal-driven next-fact selection for the closed solver loop.

The frontier answers a narrower question than the planner: *what single
unknown state should the loop try to establish next?*  It is a pure projection
of World + skill metadata.  It never mutates state and never treats an
Agent-authored claim as verified.
"""
from __future__ import annotations

from dataclasses import dataclass

from .world.models import KIND_COLLECTIONS\nfrom .skills import SkillSpec, StateSelector
from .world.models import TRUST_RANK, WorldSnapshot\n\n\ndef _selector_matches(snapshot: WorldSnapshot, selector: StateSelector) -> bool:\n    collection = getattr(snapshot, KIND_COLLECTIONS[selector.kind])\n    minimum_rank = TRUST_RANK[selector.minimum_trust]\n    for item in collection.values():\n        if not item.is_valid_at():\n            continue\n        if not __import__("fnmatch").fnmatchcase(getattr(item, "type", ""), selector.type):\n            continue\n        if selector.scope is not None and not (getattr(item, "scope", None) and __import__("fnmatch").fnmatchcase(item.scope, selector.scope)):\n            continue\n        provenance = getattr(item, "provenance", None)\n        if TRUST_RANK.get(getattr(provenance, "epistemic_status", "unclassified"), 0) >= minimum_rank:\n            return True\n    return False\n\n\ndef _selector_produced_by(selector: StateSelector, produced: StateSelector) -> bool:\n    import fnmatch\n    if selector.kind != produced.kind or not fnmatch.fnmatchcase(produced.type, selector.type):\n        return False\n    if selector.scope is not None and (produced.scope is None or not fnmatch.fnmatchcase(produced.scope, selector.scope)):\n        return False\n    return TRUST_RANK.get(produced.minimum_trust, 0) >= TRUST_RANK.get(selector.minimum_trust, 0)


@dataclass(frozen=True)
class NextFact:
    """One goal-relevant state the next solver round should try to establish."""

    goal_id: str
    selector: StateSelector
    priority: float
    producer_count: int
    depth: int = 0

    @property
    def label(self) -> str:
        scope = f" scope={self.selector.scope}" if self.selector.scope else ""
        trust = (
            f" min_trust={self.selector.minimum_trust}"
            if self.selector.minimum_trust != "claim"
            else ""
        )
        return f"{self.selector.kind}:{self.selector.type}{scope}{trust}"


def select_next_fact(
    snapshot: WorldSnapshot,
    skills: list[SkillSpec],
) -> NextFact | None:
    """Select one unmet goal state with the best deterministic utility.

    Utility favors high-priority goals, directly producible states, and states
    requiring stronger verification.  Ties are resolved by stable IDs so the
    same World revision produces the same target.
    """
    candidates: list[NextFact] = []
    for goal in snapshot.goals.values():
        if goal.status not in {"pending", "active"} or not goal.is_valid_at():
            continue
        for selector in goal.required_states:
            if _selector_matches(snapshot, selector):
                continue
            producers = [
                skill
                for skill in skills
                if any(
                    _selector_produced_by(selector, produced)
                    and TRUST_RANK.get(produced.minimum_trust, 0)
                    >= TRUST_RANK.get(selector.minimum_trust, 0)
                    for produced in skill.produces
                )
            ]
            # A goal state with no producer is still useful as a diagnostic,
            # but it should never outrank an actionable state.
            candidates.append(
                NextFact(
                    goal_id=goal.id,
                    selector=selector,
                    priority=goal.priority,
                    producer_count=len(producers),
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            -(item.priority),
            -(1 if item.producer_count else 0),
            -min(item.producer_count, 10),
            -TRUST_RANK.get(item.selector.minimum_trust, 0),
            item.goal_id,
            item.label,
        )
    )
    return candidates[0]


def render_next_fact(
    snapshot: WorldSnapshot,
    skills: list[SkillSpec],
    *,
    max_chars: int = 600,
) -> str:
    """Render a bounded next-fact control signal for Agent-facing context."""
    fact = select_next_fact(snapshot, skills)
    if fact is None:
        return "### Next fact\n- none (no unmet declared goal state)"
    status = "actionable" if fact.producer_count else "no registered producer"
    text = (
        "### Next fact\n"
        f"- {fact.label}\n"
        f"  goal={fact.goal_id} priority={fact.priority:.2f} "
        f"producers={fact.producer_count} status={status}"
    )
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + " ..."


__all__ = ["NextFact", "render_next_fact", "select_next_fact"]
