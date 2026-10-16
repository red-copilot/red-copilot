"""Goal-driven intent-fact projection and frontier planning.

Benchmark-agnostic and deterministic: facts are projected from the
authoritative World snapshot, intents are projected from skill metadata, and
the executable frontier is derived from explicit goal target states
(``Goal.required_states``). Nothing here is persisted; the projection is
recomputed on demand so the same snapshot and skills always yield the same
frontier.

When no active goal declares target states the frontier falls back to every
enabled intent, which preserves the legacy planner behavior for existing tasks.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .fact_frontier import render_next_fact\nfrom .planner import (
    HeuristicSkillPlanner,
    PlanCandidate,
    Planner,
    _selector_matches,
    selector_matches_focus_item,
)
from .progress import ProgressLedger
from .skills import SkillSpec, StateSelector
from .world.models import TRUST_RANK, WorldSnapshot, is_bookkeeping_record

# How many prerequisite hops the backward chain may follow from a goal target.
MAX_FRONTIER_DEPTH = 3
_ACTIVE_GOAL_STATUSES = frozenset({"pending", "active"})


def _selector_covers(produced: StateSelector, target: StateSelector) -> bool:
    """True when a skill-produced state can satisfy a target selector.

    ``target`` acts as the pattern (matching ``_selector_matches`` semantics);
    the produced state must meet or exceed the target's minimum trust.
    """
    if not selector_matches_focus_item(target, produced):
        return False
    return TRUST_RANK.get(produced.minimum_trust, 0) >= TRUST_RANK.get(
        target.minimum_trust, 0
    )


def _selector_signature(selector: StateSelector) -> tuple[str, str, str | None, str]:
    return (selector.kind, selector.type, selector.scope, selector.minimum_trust)


def _unmet_goal_targets(snapshot: WorldSnapshot) -> list[tuple[str, StateSelector]]:
    """Goal target states that no valid world record satisfies yet."""
    targets: list[tuple[str, StateSelector]] = []
    for goal in snapshot.goals.values():
        if goal.status not in _ACTIVE_GOAL_STATUSES or not goal.is_valid_at():
            continue
        for selector in goal.required_states:
            if not _selector_matches(snapshot, selector):
                targets.append((goal.id, selector))
    return targets


@dataclass(frozen=True)
class FactNode:
    """An observed or verified state fact in the environment."""

    id: str
    kind: str
    type: str
    scope: str | None = None
    trust: str = "claim"


@dataclass
class IntentNode:
    """A desired state transition, projected from a skill's metadata."""

    id: str
    description: str = ""
    requires: list[StateSelector] = field(default_factory=list)
    produces: list[StateSelector] = field(default_factory=list)
    status: str = "pending"  # pending | enabled | done


@dataclass(frozen=True)
class FrontierIntent:
    """An enabled intent that advances a goal, directly or as a prerequisite."""

    intent: IntentNode
    depth: int = 0
    goal_hit: bool = False


@dataclass
class IntentFactDAG:
    """Bipartite projection: facts enable intents; intents produce new facts."""

    facts: dict[str, FactNode] = field(default_factory=dict)
    intents: dict[str, IntentNode] = field(default_factory=dict)
    snapshot: WorldSnapshot | None = field(default=None, repr=False)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: WorldSnapshot,
        skills: list[SkillSpec],
    ) -> IntentFactDAG:
        """Project a world snapshot and available skills into an intent-fact DAG."""
        dag = cls(snapshot=snapshot)
        dag._ingest_facts(snapshot)
        dag._ingest_intents(skills, snapshot)
        return dag

    def _ingest_facts(self, snapshot: WorldSnapshot) -> None:
        collections = [
            ("capability", snapshot.capabilities),
            ("observation", snapshot.observations),
            ("entity", snapshot.entities),
            ("artifact", snapshot.artifacts),
            ("hypothesis", snapshot.hypotheses),
            ("goal", snapshot.goals),
        ]
        for kind, store in collections:
            for item in store.values():
                if not item.is_valid_at():
                    continue
                type_name = getattr(item, "type", kind)
                scope = getattr(item, "scope", None)
                provenance = getattr(item, "provenance", None)
                trust = getattr(provenance, "epistemic_status", "claim")
                self.facts[item.id] = FactNode(
                    id=item.id,
                    kind=kind,
                    type=type_name,
                    scope=scope,
                    trust=trust,
                )

    def _ingest_intents(self, skills: list[SkillSpec], snapshot: WorldSnapshot) -> None:
        for skill in skills:
            all_requires_met = all(_selector_matches(snapshot, req) for req in skill.requires)
            all_produces_met = bool(skill.produces) and all(
                _selector_matches(snapshot, prod) for prod in skill.produces
            )
            if all_produces_met:
                status = "done"
            elif all_requires_met:
                status = "enabled"
            else:
                status = "pending"
            self.intents[skill.id] = IntentNode(
                id=skill.id,
                description=skill.description,
                requires=list(skill.requires),
                produces=list(skill.produces),
                status=status,
            )

    def enabled_intents(self) -> list[IntentNode]:
        return [intent for intent in self.intents.values() if intent.status == "enabled"]

    def goal_targets(self) -> list[tuple[str, StateSelector]]:
        if self.snapshot is None:
            return []
        return _unmet_goal_targets(self.snapshot)

    def frontier(
        self, *, targets: list[tuple[str, StateSelector]] | None = None
    ) -> list[FrontierIntent]:
        """Return the enabled intents that advance an unmet goal target.

        Seeds are goal target states; an intent whose ``produces`` covers a seed
        is a direct goal hit. When such an intent is still pending, its unmet
        ``requires`` become new seeds one hop deeper, bounded by
        ``MAX_FRONTIER_DEPTH``. The result is deterministic and only contains
        intents whose preconditions are satisfied (``enabled``). Callers that
        already computed ``goal_targets()`` pass them as ``targets`` so the
        unmet-state scan runs once.
        """
        enabled = self.enabled_intents()
        snapshot = self.snapshot
        if targets is None:
            targets = _unmet_goal_targets(snapshot) if snapshot is not None else []
        if snapshot is None or not targets:
            return [FrontierIntent(intent=intent) for intent in _sorted_intents(enabled)]

        frontier: dict[str, FrontierIntent] = {}
        expanded: set[tuple[str, str, str | None, str, int]] = set()
        pending: deque[tuple[StateSelector, int]] = deque(
            (selector, 0) for _, selector in targets
        )
        while pending:
            selector, depth = pending.popleft()
            signature = (*_selector_signature(selector), depth)
            if signature in expanded:
                continue
            expanded.add(signature)
            for intent in self.intents.values():
                if intent.status == "done" or not any(
                    _selector_covers(produced, selector) for produced in intent.produces
                ):
                    continue
                if intent.status == "enabled":
                    _record_frontier(frontier, intent, depth=depth)
                if depth + 1 > MAX_FRONTIER_DEPTH:
                    continue
                for requirement in intent.requires:
                    if not _selector_matches(snapshot, requirement):
                        pending.append((requirement, depth + 1))

        if not frontier:
            # No skill covers the declared target: fall back to the legacy
            # all-enabled set so the loop stays live instead of stalling.
            return [FrontierIntent(intent=intent) for intent in _sorted_intents(enabled)]
        return _sorted_frontier(frontier.values())

    def render_frontier(self, *, max_chars: int = 2000) -> str:
        """Render a concise, bounded projection of the goal-driven frontier."""
        lines: list[str] = ["## Intent-Fact Frontier", ""]
        targets = self.goal_targets()
        if targets:
            lines.append("### Open goal targets")
            for goal_id, selector in targets[:8]:
                scope = f" scope={selector.scope}" if selector.scope else ""
                trust = (
                    f" min_trust={selector.minimum_trust}"
                    if selector.minimum_trust != "claim"
                    else ""
                )
                lines.append(f"- {goal_id}: {selector.kind}:{selector.type}{scope}{trust}")
            lines.append("")

        frontier = self.frontier(targets=targets)
        lines.append(f"### Actionable frontier ({len(frontier)})")
        if frontier:
            for item in frontier:
                role = "goal" if item.goal_hit else "prereq"
                lines.append(
                    f"- [{role}] {item.intent.id} (depth={item.depth}): "
                    f"{item.intent.description}"
                )
        else:
            lines.append("- none")
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            return rendered[:max_chars] + "\n... (truncated)"
        return rendered


def _sorted_intents(intents: list[IntentNode]) -> list[IntentNode]:
    return sorted(intents, key=lambda intent: intent.id)


def _record_frontier(
    frontier: dict[str, FrontierIntent],
    intent: IntentNode,
    *,
    depth: int,
) -> None:
    existing = frontier.get(intent.id)
    if existing is None:
        frontier[intent.id] = FrontierIntent(intent=intent, depth=depth, goal_hit=depth == 0)
        return
    frontier[intent.id] = replace(
        existing,
        depth=min(existing.depth, depth),
        goal_hit=existing.goal_hit or depth == 0,
    )


def _sorted_frontier(items: Iterable[FrontierIntent]) -> list[FrontierIntent]:
    return sorted(items, key=lambda item: (0 if item.goal_hit else 1, item.depth, item.intent.id))


def frontier_context(
    snapshot: WorldSnapshot,
    skills: list[SkillSpec],
    *,
    max_chars: int = 2000,
) -> str:
    """Build the bounded goal-frontier projection appended to Agent context."""
    dag = IntentFactDAG.from_snapshot(snapshot, skills)
    return dag.render_frontier(max_chars=max_chars)


def ruled_out_intents(
    snapshot: WorldSnapshot,
    progress: ProgressLedger | None = None,
) -> dict[str, list[str]]:
    """Map skill ids to why their path has already been ruled out.

    ``CONTRADICTS`` links facts to facts; nothing said which *intents* were
    tried and failed, which is what lets a run keep re-attempting the same dead
    end. Failed actions, failure records and per-skill failure counts are the
    evidence for that judgement.
    """
    reasons: dict[str, list[str]] = {}

    def record(skill_id: object, reason: str) -> None:
        if isinstance(skill_id, str) and skill_id:
            reasons.setdefault(skill_id, []).append(reason)

    for action in snapshot.actions.values():
        if action.status == "failed":
            record(action.skill_id, f"action {action.id} failed")
    for failure in snapshot.failures.values():
        attributes = failure.attributes or {}
        skill_id = attributes.get("skill_id")
        if not isinstance(skill_id, str) and failure.action_id:
            linked = snapshot.actions.get(f"action:{failure.action_id}")
            skill_id = linked.skill_id if linked is not None else None
        record(skill_id, f"failure: {failure.message}")
    if progress is not None:
        for skill_id, count in progress.failed_skill_counts.items():
            record(skill_id, f"{count} failed attempt(s)")
    return {skill_id: values[:3] for skill_id, values in reasons.items()}


def refuted_hypotheses(
    snapshot: WorldSnapshot,
    progress: ProgressLedger | None = None,
) -> list[str]:
    """Statements the run has already contradicted, from World and progress."""
    statements = [
        hypothesis.statement
        for hypothesis in snapshot.hypotheses.values()
        if hypothesis.status == "refuted"
    ]
    if progress is not None:
        statements += [
            hypothesis.statement or hypothesis.id
            for hypothesis in progress.hypotheses.values()
            if hypothesis.status == "refuted"
        ]
    return [statement for statement in statements if statement]


def round_brief(
    snapshot: WorldSnapshot,
    skills: list[SkillSpec],
    progress: ProgressLedger,
    *,
    round_number: int,
    max_chars: int = 2400,
) -> str:
    """Compile what the previous round established into the next round's prompt.

    Bounded and curated: established facts, ruled-out paths, the open frontier
    and the remaining budget - not a dump of the raw world projection.
    """
    dag = IntentFactDAG.from_snapshot(snapshot, skills)
    established = sorted(
        (
            fact
            for fact in dag.facts.values()
            if fact.trust in {"evidence", "verified"} and not is_bookkeeping_record(fact)
        ),
        key=lambda fact: fact.id,
    )
    ruled = ruled_out_intents(snapshot, progress)
    refuted = refuted_hypotheses(snapshot, progress)
    frontier = dag.frontier()

    lines = [f"## Round {round_number} brief", "", f"### Established facts ({len(established)})"]
    for fact in established[:12]:
        scope = f" scope={fact.scope}" if fact.scope else ""
        lines.append(f"- [{fact.trust}] {fact.kind}:{fact.type}{scope} (id: {fact.id})")
    if not established:
        lines.append("- none yet")

    # Agent-authored records are claims until independent evidence verifies them,
    # but they are still new knowledge the run recorded, so the next round should
    # see them (clearly labelled as unverified).
    leads = sorted(
        (
            fact
            for fact in dag.facts.values()
            if fact.trust == "claim" and not is_bookkeeping_record(fact)
        ),
        key=lambda fact: fact.id,
    )
    if leads:
        lines += ["", f"### Leads (agent-claimed, unverified) ({len(leads)})"]
        for fact in leads[:6]:
            scope = f" scope={fact.scope}" if fact.scope else ""
            lines.append(f"- {fact.kind}:{fact.type}{scope} (id: {fact.id})")

    lines += ["", f"### Ruled out ({len(ruled)} intents, {len(refuted)} hypotheses)"]
    for skill_id, why in sorted(ruled.items())[:10]:
        lines.append(f"- intent {skill_id}: {'; '.join(why)}")
    for statement in refuted[:6]:
        lines.append(f"- hypothesis: {' '.join(statement.split())[:120]}")
    if not ruled and not refuted:
        lines.append("- none yet")

    lines += ["", render_next_fact(snapshot, skills)]\n\n    lines += ["", f"### Open frontier ({len(frontier)})"]
    for item in frontier[:10]:
        role = "goal" if item.goal_hit else "prereq"
        lines.append(f"- [{role}] {item.intent.id} (depth={item.depth})")
    if not frontier:
        lines.append("- none")

    repeated = sorted(
        ((key, count) for key, count in progress.no_gain_tools.items() if count >= 3),
        key=lambda item: (-item[1], item[0]),
    )
    if repeated:
        lines += ["", "### Repeated without result"]
        for key, count in repeated[:8]:
            lines.append(f"- {key} × {count} attempts produced no new fact")

    lines += [
        "",
        "### State",
        (
            f"- model_calls={progress.model_calls} tool_calls={progress.tool_calls} "
            f"remaining_budget={progress.remaining_budget_fraction:.2f}"
        ),
        "",
        (
            "Do not repeat paths listed as ruled out. Continue from the open frontier, or "
            "explain why the frontier is wrong before trying something new."
        ),
    ]
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "\n... (truncated)"


class GoalFrontierPlanner:
    """Default candidate planner: prune to the goal frontier, then score.

    The heuristic ranker remains the scoring baseline; this planner only
    restricts candidates to the frontier and applies a deterministic
    goal/depth boost so directly goal-advancing skills rank ahead of
    prerequisites.
    """

    name = "goal-frontier"

    def __init__(self, base_planner: HeuristicSkillPlanner | None = None) -> None:
        self.base = base_planner or HeuristicSkillPlanner()

    def propose(
        self,
        snapshot: WorldSnapshot,
        skills: list[SkillSpec],
        *,
        limit: int = 5,
        progress: ProgressLedger | None = None,
    ) -> list[PlanCandidate]:
        progress = progress or ProgressLedger()
        roles = {
            item.intent.id: item
            for item in IntentFactDAG.from_snapshot(snapshot, skills).frontier()
        }
        if not roles:
            return []
        candidates = self.base.propose(
            snapshot,
            skills,
            limit=max(limit * 3, 10),
            progress=progress,
        )
        scored: list[PlanCandidate] = []
        for candidate in candidates:
            item = roles.get(candidate.skill_id)
            if item is None:
                continue
            boost = (0.20 if item.goal_hit else 0.05) - 0.05 * item.depth
            goal_relevance = 1.0 if item.goal_hit else candidate.goal_relevance
            scored.append(
                candidate.model_copy(
                    update={
                        "score": max(0.0, min(1.0, candidate.score + boost)),
                        "goal_relevance": max(candidate.goal_relevance, goal_relevance),
                    }
                )
            )
        scored.sort(key=lambda item: (-item.score, item.skill_id))
        return scored[:limit]


__all__ = [
    "MAX_FRONTIER_DEPTH",
    "FactNode",
    "FrontierIntent",
    "GoalFrontierPlanner",
    "IntentFactDAG",
    "IntentNode",
    "Planner",
    "frontier_context",
    "refuted_hypotheses",
    "round_brief",
    "ruled_out_intents",
]
