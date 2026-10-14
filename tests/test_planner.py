from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.planner import HeuristicSkillPlanner, RollingHorizonPlanner
from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.skills import SkillSpec
from harness.world import Capability, Goal, Observation, Provenance, WorldSnapshot


def test_planner_filters_unmet_requirements() -> None:
    snapshot = WorldSnapshot()
    skills = [
        SkillSpec(
            id="needs-network",
            description="Needs reachability",
            requires=[{"kind": "capability", "type": "network.reachability"}],
            produces=[{"kind": "observation", "type": "network.service"}],
            cost=0.2,
            risk=0.1,
            noise=0.2,
        )
    ]

    assert HeuristicSkillPlanner().propose(snapshot, skills) == []


def test_skill_execution_metadata_is_inert_and_scores_are_deterministic(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-be-created"
    skill = SkillSpec(
        id="inert-metadata",
        description="Planner metadata is descriptive only",
        produces=[{"kind": "observation", "type": "custom.result"}],
        metadata={"command": ["touch", str(marker)]},
    )
    planner = HeuristicSkillPlanner()
    snapshot = WorldSnapshot()

    first = planner.propose(snapshot, [skill])
    second = planner.propose(snapshot, [skill])

    assert not marker.exists()
    assert first == second
    assert first[0].skill_id == "inert-metadata"


def test_planner_explains_missing_expired_and_untrusted_skill_requirements() -> None:
    now = datetime.now(UTC)
    snapshot = WorldSnapshot(
        observations={
            "stale": Observation(
                id="stale",
                type="state.stale",
                expires_at=now - timedelta(seconds=1),
                provenance=Provenance(epistemic_status="verified"),
            ),
            "claim": Observation(
                id="claim",
                type="state.claimed",
                provenance=Provenance(epistemic_status="claim"),
            ),
        }
    )
    skills = [
        SkillSpec(
            id="missing",
            description="requires missing state",
            requires=[{"kind": "observation", "type": "state.missing"}],
        ),
        SkillSpec(
            id="stale",
            description="requires current state",
            requires=[
                {"kind": "observation", "type": "state.stale", "minimum_trust": "verified"}
            ],
        ),
        SkillSpec(
            id="untrusted",
            description="requires trusted state",
            requires=[
                {
                    "kind": "observation",
                    "type": "state.claimed",
                    "minimum_trust": "verified",
                }
            ],
        ),
    ]

    report = HeuristicSkillPlanner().explain(snapshot, skills)

    assert [item.rejection_reasons[0].split(":", 1)[0] for item in report] == [
        "missing",
        "expired",
        "insufficient_trust",
    ]
    assert all(not item.applicable for item in report)


def test_capability_gain_changes_applicable_plan() -> None:
    snapshot = WorldSnapshot()
    skill = SkillSpec(
        id="probe-service",
        description="Probe a reachable service",
        requires=[{"kind": "capability", "type": "network.reachability"}],
        produces=[{"kind": "observation", "type": "network.service"}],
    )
    planner = RollingHorizonPlanner()
    before = planner.propose(snapshot, [skill], progress=ProgressLedger())
    snapshot.capabilities["reachability"] = Capability(
        id="reachability", type="network.reachability", scope="target"
    )
    after = planner.propose(snapshot, [skill], progress=ProgressLedger())
    assert before.actions == []
    assert [action.skill_id for action in after.actions] == ["probe-service"]


def test_planner_prefers_novel_outputs() -> None:
    snapshot = WorldSnapshot(
        capabilities={
            "cap-1": Capability(
                id="cap-1",
                type="network.reachability",
                subject="agent",
                scope="target",
            )
        }
    )
    novel = SkillSpec(
        id="novel",
        description="Produce a new observation",
        requires=[{"kind": "capability", "type": "network.*"}],
        produces=[{"kind": "observation", "type": "network.service"}],
        cost=0.2,
        risk=0.1,
        noise=0.2,
    )
    redundant = SkillSpec(
        id="redundant",
        description="Produce an observation already present",
        requires=[{"kind": "capability", "type": "network.*"}],
        produces=[{"kind": "observation", "type": "network.banner"}],
        cost=0.2,
        risk=0.1,
        noise=0.2,
    )
    snapshot.observations["obs-1"] = Observation(
        id="obs-1",
        type="network.banner",
        content={"value": "known"},
    )

    candidates = HeuristicSkillPlanner().propose(snapshot, [redundant, novel])

    # A skill whose declared outputs all already hold is dropped: re-running it
    # cannot teach the solver anything new.
    assert [item.skill_id for item in candidates] == ["novel"]
    assert candidates[0].novelty == 1.0


def test_planner_ignores_expired_capabilities() -> None:
    now = datetime.now(UTC)
    snapshot = WorldSnapshot(
        capabilities={
            "cap-expired": Capability(
                id="cap-expired",
                type="network.reachability",
                subject="agent",
                scope="target",
                valid_from=now - timedelta(minutes=10),
                expires_at=now - timedelta(minutes=1),
            )
        }
    )
    skill = SkillSpec(
        id="needs-live-network",
        description="Requires a currently valid capability",
        requires=[{"kind": "capability", "type": "network.reachability"}],
        produces=[{"kind": "observation", "type": "network.service"}],
    )

    assert HeuristicSkillPlanner().propose(snapshot, [skill]) == []


def test_rolling_horizon_plan_uses_progress_and_expected_outputs() -> None:
    snapshot = WorldSnapshot(
        revision=7,
        capabilities={
            "cap-1": Capability(
                id="cap-1",
                type="network.reachability",
                subject="agent",
                scope="target",
            )
        },
    )
    skills = [
        SkillSpec(
            id="service-discovery",
            description="Enumerate reachable services",
            requires=[{"kind": "capability", "type": "network.reachability"}],
            produces=[
                {"kind": "observation", "type": "network.service"},
                {"kind": "entity", "type": "network.service"},
            ],
            cost=0.2,
            risk=0.1,
            noise=0.2,
        ),
        SkillSpec(
            id="banner-grab",
            description="Collect service banners",
            requires=[{"kind": "capability", "type": "network.reachability"}],
            produces=[{"kind": "observation", "type": "network.banner"}],
            cost=0.3,
            risk=0.1,
            noise=0.2,
        ),
    ]
    progress = ProgressLedger(
        current_subgoal="enumerate services",
        no_progress_count=2,
    )

    plan = RollingHorizonPlanner().propose(
        snapshot,
        skills,
        progress=progress,
        horizon=2,
    )

    assert plan.world_revision == 7
    assert plan.horizon == 2
    assert plan.current_subgoal == "enumerate services"
    assert plan.replan_required is True
    assert "no_progress_threshold" in plan.replan_reasons
    assert len(plan.actions) == 2
    assert plan.actions[0].rank == 1
    assert plan.actions[0].expected_observations
    assert "no_progress_threshold" in plan.actions[0].replan_triggers
    assert "subgoal=enumerate services" in plan.actions[0].rationale
    assert "focus_relevance=" in plan.actions[0].rationale


def _focus_snapshot(observations: dict) -> WorldSnapshot:
    return WorldSnapshot(
        goals={
            "goal-1": Goal(
                id="goal-1",
                description="Find the admin panel",
                status="active",
                priority=1.0,
            )
        },
        observations=observations,
    )


def _focus_skills() -> list[SkillSpec]:
    return [
        SkillSpec(
            id="confirm-endpoint",
            description="Confirm the web endpoint",
            produces=[
                {"kind": "observation", "type": "web.endpoint"},
                {"kind": "observation", "type": "meta.note"},
            ],
            cost=0.2,
            risk=0.2,
            noise=0.2,
        ),
        SkillSpec(
            id="dns-lookup",
            description="Resolve the hostname",
            produces=[
                {"kind": "observation", "type": "dns.record"},
                {"kind": "observation", "type": "dns.zone"},
            ],
            cost=0.2,
            risk=0.2,
            noise=0.2,
        ),
    ]


def _lead(record_id: str, type_name: str, confidence: float, **content) -> Observation:
    return Observation(
        id=record_id,
        type=type_name,
        content=content,
        confidence=confidence,
        provenance=Provenance(epistemic_status="claim"),
    )


def _frontier_before() -> WorldSnapshot:
    # Two unverified leads. The web endpoint lead outranks the dns zone lead on
    # goal relevance, so it heads the frontier.
    return _focus_snapshot(
        {
            "lead": _lead("lead", "web.endpoint", 0.3, path="/admin"),
            "zone": _lead("zone", "dns.zone", 0.3, name="target"),
        }
    )


def _frontier_after() -> WorldSnapshot:
    now = datetime.now(UTC)
    # The web endpoint lead is confirmed (and displaced); the dns zone lead has
    # expired; a fresh dns record lead now heads the frontier.
    return _focus_snapshot(
        {
            "lead": _lead("lead", "web.endpoint", 0.3, path="/admin"),
            "zone": Observation(
                id="zone",
                type="dns.zone",
                content={"name": "target"},
                confidence=0.3,
                provenance=Provenance(epistemic_status="claim"),
                expires_at=now - timedelta(seconds=1),
            ),
            "confirmed": Observation(
                id="confirmed",
                type="web.endpoint",
                content={"path": "/admin", "status": 200},
                provenance=Provenance(epistemic_status="verified"),
                supersedes=["lead"],
            ),
            "dns-lead": _lead("dns-lead", "dns.record", 0.3, name="target"),
        }
    )


def test_focus_relevance_prefers_the_skill_that_resolves_the_frontier() -> None:
    candidates = HeuristicSkillPlanner().propose(_frontier_before(), _focus_skills())

    by_id = {candidate.skill_id: candidate for candidate in candidates}
    # Equal novelty, so the frontier signal alone decides the ranking.
    assert by_id["confirm-endpoint"].novelty == by_id["dns-lookup"].novelty
    assert by_id["confirm-endpoint"].focus_relevance == 0.5
    assert by_id["dns-lookup"].focus_relevance == 0.0
    assert candidates[0].skill_id == "confirm-endpoint"


def test_focus_relevance_follows_changes_in_the_world_frontier() -> None:
    skills = _focus_skills()
    before = HeuristicSkillPlanner().propose(_frontier_before(), skills)
    after = HeuristicSkillPlanner().propose(_frontier_after(), skills)

    before_by_id = {candidate.skill_id: candidate for candidate in before}
    after_by_id = {candidate.skill_id: candidate for candidate in after}
    assert before_by_id["confirm-endpoint"].focus_relevance > before_by_id["dns-lookup"].focus_relevance
    assert after_by_id["dns-lookup"].focus_relevance > after_by_id["confirm-endpoint"].focus_relevance
    assert before[0].skill_id == "confirm-endpoint"
    assert after[0].skill_id == "dns-lookup"


def test_rolling_horizon_clamps_to_three_actions() -> None:
    snapshot = WorldSnapshot()
    skills = [
        SkillSpec(
            id=f"skill-{index}",
            description=f"Skill {index}",
            produces=[{"kind": "observation", "type": f"obs.{index}"}],
        )
        for index in range(5)
    ]

    plan = RollingHorizonPlanner().propose(snapshot, skills, horizon=20)

    assert plan.horizon == 3
    assert len(plan.actions) == 3


def test_rolling_horizon_consumes_verifier_replan_state() -> None:
    snapshot = WorldSnapshot()
    progress = ProgressLedger(
        replan_reasons=["expected_observation_missing"],
        last_verification={
            "status": "inconclusive",
            "expected_observation": "admin endpoint exists",
            "actual_observation": None,
            "evidence": [],
            "replan_required": True,
            "replan_reasons": ["expected_observation_missing"],
        },
    )

    plan = RollingHorizonPlanner().propose(
        snapshot,
        [],
        progress=progress,
        horizon=1,
    )

    assert plan.replan_required is True
    assert plan.replan_reasons == ["expected_observation_missing"]
    assert plan.previous_verification_status == "inconclusive"


def test_hypothesis_contradiction_changes_plan_decision() -> None:
    progress = ProgressLedger(
        hypotheses={
            "hyp-1": {
                "id": "hyp-1",
                "statement": "service is exposed",
                "status": "refuted",
                "evidence_against": ["evidence-1"],
            }
        }
    )
    skill = SkillSpec(
        id="alternate-service-check",
        description="Check another service after the current hypothesis is refuted",
        produces=[{"kind": "observation", "type": "network.service"}],
    )
    plan = RollingHorizonPlanner().propose(WorldSnapshot(), [skill], progress=progress, horizon=1)
    assert plan.replan_required
    assert "hypothesis_contradicted" in plan.replan_reasons
    assert "hypothesis_contradicted" in plan.actions[0].replan_triggers


def test_rejected_submission_becomes_planner_replan_signal() -> None:
    progress = ProgressLedger()
    progress.record_submission(accepted=False, completed=False)

    assert progress.rejected_submissions == 1
    assert progress.no_progress_count == 1
    assert "benchmark_negative_feedback" in progress.replan_reasons

    skill = SkillSpec(
        id="alternate-path",
        description="Try an alternate path after negative benchmark feedback",
        produces=[{"kind": "observation", "type": "alternate.evidence"}],
    )
    plan = RollingHorizonPlanner().propose(
        WorldSnapshot(),
        [skill],
        progress=progress,
        horizon=1,
    )

    assert plan.replan_required is True
    assert "benchmark_negative_feedback" in plan.replan_reasons
    assert "benchmark_negative_feedback" in plan.actions[0].replan_triggers


def test_goal_state_changes_candidate_order() -> None:
    skills = [
        SkillSpec(
            id="web-routes",
            description="Enumerate web routes and discover exposed endpoints",
            domain="web",
            produces=[{"kind": "observation", "type": "web.endpoint"}],
        ),
        SkillSpec(
            id="binary-strings",
            description="Extract strings and inspect binary firmware",
            domain="binary",
            produces=[{"kind": "artifact", "type": "binary.strings"}],
        ),
    ]
    web_snapshot = WorldSnapshot(
        goals={
            "goal:1": Goal(
                id="goal:1",
                description="Discover exposed web routes and endpoints",
                status="active",
            )
        }
    )
    binary_snapshot = WorldSnapshot(
        goals={
            "goal:2": Goal(
                id="goal:2",
                description="Inspect binary firmware and extract strings",
                status="active",
            )
        }
    )
    planner = HeuristicSkillPlanner()

    assert planner.propose(web_snapshot, skills)[0].skill_id == "web-routes"
    assert planner.propose(binary_snapshot, skills)[0].skill_id == "binary-strings"


def test_planner_penalizes_failed_skill_and_excludes_over_budget_actions() -> None:
    skills = [
        SkillSpec(id="repeated-failure", description="Retry target probe", cost=0.2),
        SkillSpec(id="alternate", description="Inspect another target", cost=0.2),
    ]
    progress = ProgressLedger(failed_skill_counts={"repeated-failure": 3})
    candidates = HeuristicSkillPlanner().propose(WorldSnapshot(), skills, progress=progress)

    assert candidates[0].skill_id == "alternate"
    assert candidates[1].failure_penalty == 1.0

    limited = ProgressLedger(budget_limits={"max_tool_calls": 1})
    limited.record_event(AgentEvent(type="tool.call", data={"tool": "bash"}))
    assert HeuristicSkillPlanner().propose(WorldSnapshot(), skills, progress=limited) == []


def test_planner_can_require_verified_world_state() -> None:
    skill = SkillSpec(
        id="exploit-confirmed-endpoint",
        description="Continue from a verified endpoint",
        requires=[
            {
                "kind": "observation",
                "type": "web.endpoint",
                "minimum_trust": "verified",
            }
        ],
        produces=[{"kind": "observation", "type": "web.finding"}],
    )
    snapshot = WorldSnapshot(
        observations={
            "claimed": Observation(
                id="claimed",
                type="web.endpoint",
                provenance=Provenance(actor="agent:test", epistemic_status="claim"),
            )
        }
    )

    assert HeuristicSkillPlanner().propose(snapshot, [skill]) == []

    snapshot.observations["verified"] = Observation(
        id="verified",
        type="web.endpoint",
        provenance=Provenance(actor="harness", epistemic_status="verified"),
    )
    assert HeuristicSkillPlanner().propose(snapshot, [skill])
