"""Unit tests for the goal-driven intent-fact projection and frontier planning."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from harness.benchmark.base import Submission
from harness.benchmark.zc_adapter import ZCBenchmarkAdapter, ZCClient
from harness.cognition import (
    MAX_FRONTIER_DEPTH,
    GoalFrontierPlanner,
    IntentFactDAG,
    frontier_context,
    refuted_hypotheses,
    round_brief,
    ruled_out_intents,
)
from harness.progress import ProgressLedger
from harness.runtime.solver_profile import (
    DEFAULT_SOLVER_PROFILE,
    SolverProfile,
    solver_profile_candidate,
    solver_profile_settings,
)
from harness.skills import SkillSpec, load_skills
from harness.world.models import (
    ActionRecord,
    Capability,
    Entity,
    Failure,
    Goal,
    Hypothesis,
    Observation,
    Provenance,
    WorldSnapshot,
)


def _skill(
    skill_id: str,
    *,
    requires: list[dict] | None = None,
    produces: list[dict] | None = None,
) -> SkillSpec:
    return SkillSpec(
        id=skill_id,
        description=f"{skill_id} description",
        requires=requires or [],
        produces=produces or [],
    )


def _goal(*selectors: dict, status: str = "active") -> Goal:
    return Goal(
        id="goal:test",
        description="Reach the declared target states",
        status=status,
        priority=1.0,
        required_states=[dict(selector) for selector in selectors],
    )


def _reachability() -> dict:
    return {
        "cap1": Capability(
            id="cap1",
            type="network.reachability",
            provenance=Provenance(epistemic_status="evidence"),
        )
    }


class TestDAGConstruction:
    def test_empty_snapshot_yields_empty_dag(self):
        dag = IntentFactDAG.from_snapshot(WorldSnapshot(), [])
        assert len(dag.facts) == 0
        assert len(dag.intents) == 0

    def test_facts_from_capabilities(self):
        snap = WorldSnapshot(capabilities=_reachability())
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        assert any(fact.type == "network.reachability" for fact in dag.facts.values())

    def test_intent_status_without_goal(self):
        snap = WorldSnapshot(capabilities=_reachability())
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        assert dag.intents["recon.active"].status == "enabled"
        assert dag.intents["discovery.vuln-scan"].status == "pending"


class TestGoalDrivenFrontier:
    def test_no_goal_targets_falls_back_to_all_enabled(self):
        snap = WorldSnapshot(capabilities=_reachability())
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        frontier_ids = {item.intent.id for item in dag.frontier()}
        assert frontier_ids == {intent.id for intent in dag.enabled_intents()}
        assert frontier_ids

    def test_backward_chain_finds_only_enabled_prerequisite(self):
        snap = WorldSnapshot(
            capabilities=_reachability(),
            goals={"goal:test": _goal({"kind": "observation", "type": "vuln.confirmed"})},
        )
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        frontier = {item.intent.id: item for item in dag.frontier()}
        assert "exploit.vuln-verify" not in frontier
        assert "recon.active" in frontier
        assert frontier["recon.active"].goal_hit is False
        assert frontier["recon.active"].depth == MAX_FRONTIER_DEPTH
        assert all(item.depth <= MAX_FRONTIER_DEPTH for item in dag.frontier())

    def test_direct_goal_hit_when_precondition_met(self):
        snap = WorldSnapshot(
            observations={
                "obs1": Observation(
                    id="obs1",
                    type="web.finding",
                    provenance=Provenance(epistemic_status="claim"),
                )
            },
            goals={"goal:test": _goal({"kind": "observation", "type": "vuln.confirmed"})},
        )
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        frontier = {item.intent.id: item for item in dag.frontier()}
        assert frontier["exploit.vuln-verify"].goal_hit is True
        assert frontier["exploit.vuln-verify"].depth == 0

    def test_frontier_is_deterministic(self):
        snap = WorldSnapshot(
            capabilities=_reachability(),
            goals={"goal:test": _goal({"kind": "observation", "type": "vuln.confirmed"})},
        )
        skills = load_skills("skills/offensive")
        first = IntentFactDAG.from_snapshot(snap, skills).frontier()
        second = IntentFactDAG.from_snapshot(snap, skills).frontier()
        assert [(i.intent.id, i.depth, i.goal_hit) for i in first] == [
            (i.intent.id, i.depth, i.goal_hit) for i in second
        ]

    def test_cyclic_requirements_terminate(self):
        goal = _goal({"kind": "observation", "type": "obs.g"})
        skills = [
            _skill(
                "g",
                requires=[{"kind": "observation", "type": "obs.a"}],
                produces=[{"kind": "observation", "type": "obs.g"}],
            ),
            _skill(
                "a",
                requires=[{"kind": "observation", "type": "obs.b"}],
                produces=[{"kind": "observation", "type": "obs.a"}],
            ),
            _skill(
                "b",
                requires=[{"kind": "observation", "type": "obs.a"}],
                produces=[{"kind": "observation", "type": "obs.b"}],
            ),
        ]
        dag = IntentFactDAG.from_snapshot(WorldSnapshot(goals={"goal:test": goal}), skills)
        assert dag.frontier() == []

    def test_enabled_prerequisite_within_depth_bound(self):
        goal = _goal({"kind": "observation", "type": "obs.g"})
        skills = [
            _skill(
                "s-goal",
                requires=[{"kind": "observation", "type": "obs.a"}],
                produces=[{"kind": "observation", "type": "obs.g"}],
            ),
            _skill(
                "s-a",
                requires=[{"kind": "observation", "type": "obs.b"}],
                produces=[{"kind": "observation", "type": "obs.a"}],
            ),
            _skill(
                "s-b",
                requires=[{"kind": "observation", "type": "obs.c"}],
                produces=[{"kind": "observation", "type": "obs.b"}],
            ),
            _skill("s-c", produces=[{"kind": "observation", "type": "obs.c"}]),
        ]
        dag = IntentFactDAG.from_snapshot(WorldSnapshot(goals={"goal:test": goal}), skills)
        frontier = {item.intent.id: item for item in dag.frontier()}
        assert set(frontier) == {"s-c"}
        assert frontier["s-c"].goal_hit is False
        assert frontier["s-c"].depth == MAX_FRONTIER_DEPTH


class TestGoalTargetTrust:
    def test_claim_does_not_satisfy_evidence_target(self):
        goal = _goal({"kind": "observation", "type": "vuln.confirmed", "minimum_trust": "evidence"})
        snap = WorldSnapshot(
            observations={
                "obs1": Observation(
                    id="obs1",
                    type="vuln.confirmed",
                    provenance=Provenance(epistemic_status="claim"),
                )
            },
            goals={"goal:test": goal},
        )
        assert IntentFactDAG.from_snapshot(snap, []).goal_targets() == [
            ("goal:test", goal.required_states[0])
        ]

    def test_evidence_satisfies_evidence_target(self):
        goal = _goal({"kind": "observation", "type": "vuln.confirmed", "minimum_trust": "evidence"})
        snap = WorldSnapshot(
            observations={
                "obs1": Observation(
                    id="obs1",
                    type="vuln.confirmed",
                    provenance=Provenance(epistemic_status="evidence"),
                )
            },
            goals={"goal:test": goal},
        )
        assert IntentFactDAG.from_snapshot(snap, []).goal_targets() == []

    def test_expired_fact_does_not_satisfy_target(self):
        goal = _goal({"kind": "observation", "type": "web.finding"})
        snap = WorldSnapshot(
            observations={
                "obs1": Observation(
                    id="obs1",
                    type="web.finding",
                    expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    provenance=Provenance(epistemic_status="evidence"),
                )
            },
            goals={"goal:test": goal},
        )
        assert IntentFactDAG.from_snapshot(snap, []).goal_targets()

    def test_inactive_goal_does_not_drive_frontier(self):
        goal = _goal({"kind": "observation", "type": "web.finding"}, status="completed")
        snap = WorldSnapshot(
            capabilities=_reachability(),
            goals={"goal:test": goal},
        )
        dag = IntentFactDAG.from_snapshot(snap, load_skills("skills/offensive"))
        assert {item.intent.id for item in dag.frontier()} == {
            intent.id for intent in dag.enabled_intents()
        }


class TestGoalFrontierPlanner:
    def test_prunes_to_frontier_only(self):
        snap = WorldSnapshot(
            capabilities=_reachability(),
            observations={
                "ep": Observation(
                    id="ep",
                    type="web.endpoint",
                    provenance=Provenance(epistemic_status="claim"),
                )
            },
            entities={
                "ep-entity": Entity(
                    id="ep-entity",
                    type="web.endpoint",
                    provenance=Provenance(epistemic_status="claim"),
                )
            },
            goals={"goal:test": _goal({"kind": "observation", "type": "web.finding"})},
        )
        candidates = GoalFrontierPlanner().propose(snap, load_skills("skills/offensive"), limit=5)
        assert candidates
        skill_ids = {candidate.skill_id for candidate in candidates}
        # Goal-advancing discovery skills only; unrelated enabled recon is pruned.
        assert skill_ids == {"discovery.vuln-scan", "discovery.logic-flaw"}
        assert "recon.passive" not in skill_ids
        # Direct goal hits are surfaced as goal-relevant, ranked ahead of prereqs.
        assert all(candidate.goal_relevance == 1.0 for candidate in candidates)

    def test_direct_goal_hit_ranks_first(self):
        snap = WorldSnapshot(
            capabilities=_reachability(),
            observations={
                "ep": Observation(
                    id="ep",
                    type="web.endpoint",
                    provenance=Provenance(epistemic_status="claim"),
                )
            },
            goals={"goal:test": _goal({"kind": "observation", "type": "web.finding"})},
        )
        candidates = GoalFrontierPlanner().propose(snap, load_skills("skills/offensive"), limit=5)
        assert candidates[0].goal_relevance == 1.0


class TestFrontierContext:
    def test_renders_and_is_bounded(self):
        snap = WorldSnapshot(
            capabilities=_reachability(),
            goals={"goal:test": _goal({"kind": "observation", "type": "vuln.confirmed"})},
        )
        rendered = frontier_context(snap, load_skills("skills/offensive"), max_chars=4000)
        assert "## Intent-Fact Frontier" in rendered
        assert "### Open goal targets" in rendered
        assert "### Actionable frontier" in rendered

        truncated = frontier_context(snap, load_skills("skills/offensive"), max_chars=60)
        assert truncated.endswith("(truncated)")
        assert len(truncated) <= 60 + len("\n... (truncated)")


class TestSolverProfiles:
    def test_goal_profile_is_default_and_selects_frontier(self):
        assert DEFAULT_SOLVER_PROFILE is SolverProfile.PI_WORLD_GOAL
        assert solver_profile_candidate(DEFAULT_SOLVER_PROFILE) == "goal-frontier"
        assert solver_profile_settings(SolverProfile.PI_WORLD_GOAL) == (True, True)
        assert solver_profile_candidate(SolverProfile.PI_WORLD_HEURISTIC) == "heuristic"
        assert solver_profile_settings(SolverProfile.PI_WORLD_HEURISTIC) == (True, True)
        assert solver_profile_settings(SolverProfile.PI_WORLD) == (True, False)


class TestZCPlatformIntegration:
    def test_zc_client_mock_mode(self):
        async def scenario():
            client = ZCClient("mock-token")
            projects = await client.search_projects()
            assert len(projects) > 0
            assert projects[0].code == "proj-hackathon-t3"

            route = await client.create_gateway_route()
            assert "ai_gateway" in route.gateway_url
            assert route.session_id.startswith("zc-session-")

            sub = await client.submit_vuln(
                {"target": "https://demo.target.zc.internal", "poc": "test"}
            )
            assert sub.status == "pending_triage"
            assert sub.vuln_code.startswith("VULN-")

            sessions = await client.gateway_sessions(route.session_id)
            assert sessions["model_calls"] > 0
            await client.close()

        asyncio.run(scenario())

    def test_zc_benchmark_adapter_lifecycle(self):
        async def scenario():
            client = ZCClient("mock-token")
            adapter = ZCBenchmarkAdapter(client)

            cases = await adapter.discover()
            assert len(cases) > 0
            case = cases[0]
            assert case.benchmark == "zc"

            session = await adapter.provision(case)
            assert session.benchmark == "zc"
            assert len(session.targets) > 0
            assert "zc-session-" in session.metadata["gateway_session_id"]

            vuln_sub = Submission(
                type="vuln_report",
                value="https://demo.target.zc.internal/api/search",
                metadata={
                    "title": "SQL Injection",
                    "vuln_type": "web.sqli",
                    "poc": "id=1' OR 1=1--",
                },
            )
            sub_res = await adapter.submit(session, vuln_sub)
            assert sub_res.accepted is True
            assert sub_res.score_delta > 0

            eval_res = await adapter.evaluate(session)
            assert eval_res.success is True
            assert eval_res.score > 0
            assert eval_res.milestones["has_accepted_submission"] is True

            await adapter.teardown(session)

        asyncio.run(scenario())


class TestRuledOutAndRoundBrief:
    def _graph(self) -> tuple[WorldSnapshot, list[SkillSpec], ProgressLedger]:
        skills = load_skills("skills/offensive")
        snapshot = WorldSnapshot(
            capabilities={
                "cap1": Capability(
                    id="cap1",
                    type="network.reachability",
                    provenance=Provenance(epistemic_status="evidence"),
                )
            },
            actions={
                "action:c1": ActionRecord(
                    id="action:c1",
                    type="bash",
                    status="failed",
                    attributes={"skill_id": "discovery.vuln-scan"},
                )
            },
            failures={
                "f1": Failure(
                    id="f1",
                    type="tool.error",
                    message="connection reset",
                    action_id="c1",
                )
            },
            hypotheses={
                "h1": Hypothesis(
                    id="h1",
                    statement="the endpoint is vulnerable to LFI",
                    confidence=0.2,
                    status="refuted",
                )
            },
        )
        progress = ProgressLedger(failed_skill_counts={"discovery.vuln-scan": 2})
        return snapshot, skills, progress

    def test_ruled_out_intents_link_failures_to_skills(self):
        snapshot, _, progress = self._graph()
        ruled = ruled_out_intents(snapshot, progress)
        assert "discovery.vuln-scan" in ruled
        reasons = " ".join(ruled["discovery.vuln-scan"])
        assert "failed" in reasons

    def test_refuted_hypotheses_are_collected(self):
        snapshot, _, progress = self._graph()
        assert refuted_hypotheses(snapshot, progress) == [
            "the endpoint is vulnerable to LFI"
        ]

    def test_round_brief_carries_facts_ruled_out_and_frontier(self):
        snapshot, skills, progress = self._graph()
        brief = round_brief(snapshot, skills, progress, round_number=2)
        assert "## Round 2 brief" in brief
        assert "### Established facts" in brief
        assert "cap1" in brief
        assert "### Ruled out" in brief
        assert "discovery.vuln-scan" in brief
        assert "the endpoint is vulnerable to LFI" in brief
        assert "### Open frontier" in brief
        assert "Do not repeat paths listed as ruled out" in brief

    def test_round_brief_is_bounded(self):
        snapshot, skills, progress = self._graph()
        brief = round_brief(snapshot, skills, progress, round_number=3, max_chars=200)
        assert brief.endswith("(truncated)")
        assert len(brief) <= 200 + len("\n... (truncated)")


class TestRepeatedWithoutResult:
    def test_brief_reports_repeated_attempts_with_no_new_fact(self) -> None:
        snapshot, skills, _ = TestRuledOutAndRoundBrief()._graph()
        progress = ProgressLedger(no_gain_tools={"bash:deadbeef": 6, "nmap": 1})
        brief = round_brief(snapshot, skills, progress, round_number=4)
        assert "### Repeated without result" in brief
        assert "bash:deadbeef × 6 attempts produced no new fact" in brief
        # Below the threshold it is noise, not a finding.
        assert "nmap × 1" not in brief

    def test_brief_excludes_harness_tool_bookkeeping_facts(self) -> None:
        skills = load_skills("skills/offensive")
        snapshot = WorldSnapshot(
            observations={
                "obs:tool:call_1": Observation(
                    id="obs:tool:call_1",
                    type="tool.execution",
                    provenance=Provenance(epistemic_status="evidence"),
                ),
                "obs:real": Observation(
                    id="obs:real",
                    type="web.endpoint",
                    provenance=Provenance(epistemic_status="evidence"),
                ),
            }
        )
        brief = round_brief(snapshot, skills, ProgressLedger(), round_number=2)
        assert "obs:real" in brief
        assert "obs:tool:call_1" not in brief


def test_brief_lists_agent_claims_as_unverified_leads() -> None:
    skills = load_skills("skills/offensive")
    snapshot = WorldSnapshot(
        observations={
            "obs:tool:call_1": Observation(
                id="obs:tool:call_1",
                type="tool.execution",
                provenance=Provenance(actor="harness", epistemic_status="evidence"),
            ),
            "obs:lead": Observation(
                id="obs:lead",
                type="web.endpoint",
                provenance=Provenance(actor="agent:pi", epistemic_status="claim"),
            ),
        }
    )
    brief = round_brief(snapshot, skills, ProgressLedger(), round_number=2)
    assert "### Leads (agent-claimed, unverified) (1)" in brief
    assert "obs:lead" in brief
    # Harness tool bookkeeping is neither a fact nor a lead.
    assert "obs:tool:call_1" not in brief

