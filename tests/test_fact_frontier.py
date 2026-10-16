from harness.fact_frontier import render_next_fact, select_next_fact
from harness.skills import SkillSpec
from harness.world.models import Goal, Observation, Provenance, StateSelector, WorldSnapshot


def test_next_fact_prefers_actionable_high_priority_goal() -> None:
    target = StateSelector(kind="observation", type="domain.controller", minimum_trust="verified")
    snapshot = WorldSnapshot(
        goals={
            "goal-low": Goal(id="goal-low", description="low", priority=0.2),
            "goal-high": Goal(
                id="goal-high",
                description="high",
                priority=0.9,
                required_states=[target],
            ),
        }
    )
    skills = [
        SkillSpec(
            id="probe-dc",
            description="identify domain controller",
            produces=[StateSelector(kind="observation", type="domain.controller", minimum_trust="verified")],
        )
    ]

    selected = select_next_fact(snapshot, skills)

    assert selected is not None
    assert selected.goal_id == "goal-high"
    assert selected.selector == target
    assert selected.producer_count == 1
    assert "domain.controller" in render_next_fact(snapshot, skills)


def test_agent_claim_does_not_close_verified_next_fact() -> None:
    target = StateSelector(kind="observation", type="admin.route", minimum_trust="verified")
    snapshot = WorldSnapshot(
        goals={
            "goal": Goal(
                id="goal",
                description="find admin route",
                priority=1.0,
                required_states=[target],
            )
        },
        observations={
            "claim": Observation(
                id="claim",
                type="admin.route",
                content={"path": "/admin"},
                confidence=1.0,
                provenance=Provenance(actor="agent:test", epistemic_status="claim"),
            )
        },
    )

    selected = select_next_fact(snapshot, [])

    assert selected is not None
    assert selected.selector == target


def test_next_fact_is_none_when_goal_state_is_verified() -> None:
    target = StateSelector(kind="observation", type="admin.route", minimum_trust="verified")
    snapshot = WorldSnapshot(
        goals={
            "goal": Goal(
                id="goal",
                description="find admin route",
                priority=1.0,
                required_states=[target],
            )
        },
        observations={
            "verified": Observation(
                id="verified",
                type="admin.route",
                content={"path": "/admin"},
                confidence=1.0,
                provenance=Provenance(actor="harness", epistemic_status="verified"),
            )
        },
    )

    assert select_next_fact(snapshot, []) is None
