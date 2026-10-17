"""Regression baseline for the retired intent-fact frontier.

The goal-driven frontier left the package in favour of an agent-native,
file-system cognition layer. The implementation is frozen under ``tests/ablation``
so the ablation recorded in ``docs/agent-architecture-v2.md`` stays
reproducible: the same snapshot and skills must still produce the same
deterministic frontier (goal hit first, then backward-chained prerequisites,
with an all-enabled fallback when no goal declares targets).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ablation.cognition import IntentFactDAG
from ablation.selectors import StateSelector
from ablation.skills import SkillSpec


@dataclass(frozen=True)
class _Provenance:
    epistemic_status: str = "claim"


@dataclass
class _Record:
    id: str
    type: str
    scope: str | None = None
    provenance: _Provenance = field(default_factory=_Provenance)

    def is_valid_at(self) -> bool:
        return True


@dataclass
class _Goal:
    id: str
    description: str
    status: str = "active"
    required_states: list[StateSelector] = field(default_factory=list)

    def is_valid_at(self) -> bool:
        return True


@dataclass
class _Snapshot:
    revision: int = 1
    goals: dict[str, _Goal] = field(default_factory=dict)
    entities: dict[str, _Record] = field(default_factory=dict)
    observations: dict[str, _Record] = field(default_factory=dict)
    capabilities: dict[str, _Record] = field(default_factory=dict)
    artifacts: dict[str, _Record] = field(default_factory=dict)
    hypotheses: dict[str, _Record] = field(default_factory=dict)
    failures: dict[str, _Record] = field(default_factory=dict)
    actions: dict[str, _Record] = field(default_factory=dict)
    constraints: dict[str, _Record] = field(default_factory=dict)
    relations: dict[str, _Record] = field(default_factory=dict)


def _skill(skill_id: str, requires, produces) -> SkillSpec:
    return SkillSpec(
        id=skill_id,
        domain="web",
        description=skill_id,
        requires=list(requires),
        produces=list(produces),
    )


def _observation(type_name: str) -> _Record:
    return _Record(id=f"obs:{type_name}", type=type_name, provenance=_Provenance("evidence"))


def test_frontier_prefers_the_direct_goal_hit() -> None:
    snapshot = _Snapshot(
        goals={
            "goal:objective": _Goal(
                id="goal:objective",
                description="confirm the vulnerability",
                required_states=[StateSelector(kind="observation", type="vuln.confirmed")],
            )
        },
        observations={"obs:web.finding": _observation("web.finding")},
    )
    skills = [
        _skill(
            "exploit.vuln-verify",
            [StateSelector(kind="observation", type="web.finding")],
            [StateSelector(kind="observation", type="vuln.confirmed")],
        ),
        _skill(
            "discovery.vuln-scan",
            [],
            [StateSelector(kind="observation", type="web.finding")],
        ),
    ]

    frontier = IntentFactDAG.from_snapshot(snapshot, skills).frontier()

    assert [item.intent.id for item in frontier] == ["exploit.vuln-verify"]
    assert frontier[0].goal_hit is True
    assert frontier[0].depth == 0


def test_frontier_walks_back_to_an_enabled_prerequisite() -> None:
    snapshot = _Snapshot(
        goals={
            "goal:objective": _Goal(
                id="goal:objective",
                description="confirm the vulnerability",
                required_states=[StateSelector(kind="observation", type="vuln.confirmed")],
            )
        },
        observations={"obs:web.endpoint": _observation("web.endpoint")},
    )
    skills = [
        _skill(
            "exploit.vuln-verify",
            [StateSelector(kind="observation", type="web.finding")],
            [StateSelector(kind="observation", type="vuln.confirmed")],
        ),
        _skill(
            "discovery.vuln-scan",
            [StateSelector(kind="observation", type="web.endpoint")],
            [StateSelector(kind="observation", type="web.finding")],
        ),
    ]
    dag = IntentFactDAG.from_snapshot(snapshot, skills)

    frontier = dag.frontier()
    assert [item.intent.id for item in frontier] == ["discovery.vuln-scan"]
    assert frontier[0].depth == 1
    assert frontier[0].goal_hit is False
    assert [hint.intent.id for hint in dag.next_unlocks()] == ["exploit.vuln-verify"]


def test_frontier_falls_back_to_every_enabled_intent_without_goal_targets() -> None:
    snapshot = _Snapshot(
        goals={"goal:objective": _Goal(id="goal:objective", description="explore freely")},
        capabilities={"cap:reach": _Record(id="cap:reach", type="network.reachability")},
    )
    skills = [
        _skill(
            "recon.passive",
            [StateSelector(kind="capability", type="network.reachability")],
            [StateSelector(kind="observation", type="network.service")],
        )
    ]

    frontier = IntentFactDAG.from_snapshot(snapshot, skills).frontier()

    assert [item.intent.id for item in frontier] == ["recon.passive"]
