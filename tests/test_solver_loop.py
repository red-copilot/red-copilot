from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.skills import SkillSpec
from harness.solver_loop import SolverLoop
from harness.solver_policy import SolverAction
from harness.trace import TraceRecorder
from harness.verification import ActionVerdict, EvidenceRef, VerifierRegistry
from harness.world import SQLiteWorldRepository


class FakeSession:
    def __init__(self) -> None:
        self.feedback = []

    async def observe(self, observation) -> None:
        self.feedback.append(observation)


def test_solver_loop_applies_aci_updates_world_without_action_id_verdict(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-test", "task-test")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find admin endpoint",
        planned_world_revision=world.snapshot.revision,
    )

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="action.intent",
                data={
                    "description": "probe admin",
                    "expected_observations": ["admin endpoint exists"],
                },
            ),
        )
    )
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="world.observe",
                data={
                    "id": "obs-admin",
                    "type": "web.endpoint",
                    "summary": "admin endpoint exists",
                    "content": {"path": "/admin", "status": 200},
                    "confidence": 1.0,
                },
            ),
        )
    )

    assert "obs-admin" in world.snapshot.observations
    assert loop.stats.aci_accepted == 2
    assert loop.stats.aci_world_mutations == 1
    # No action verdict is emitted until a tool call has a traceable action ID.
    assert progress.last_verification is None

    feedback_types = [item.type for item in session.feedback]
    assert "aci.feedback" in feedback_types
    assert "world.state.updated" in feedback_types
    assert "solver.verification" not in feedback_types
    context = (tmp_path / "world.context.txt").read_text(encoding="utf-8")
    assert "obs-admin" in context
    assert context.startswith("## Focus")
    assert "reference: find admin endpoint" in context
    assert "- [claim] observation:web.endpoint" in context


def test_solver_loop_rejects_invalid_typed_event_without_stopping(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-test", "task-test")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="test invalid event",
        planned_world_revision=world.snapshot.revision,
    )

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="world.observe",
                data={"id": "bad-observation"},
            ),
        )
    )

    assert loop.stats.aci_rejected == 1
    assert world.snapshot.revision == 0
    rejected = next(
        item
        for item in session.feedback
        if item.type == "aci.feedback" and item.data["accepted"] is False
    )
    assert rejected.data["event_type"] == "world.observe"


def test_solver_loop_correlates_legacy_tool_events_without_ids(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-legacy", "task-legacy")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="legacy tool event compatibility",
        planned_world_revision=world.snapshot.revision,
    )

    async def scenario() -> None:
        await loop.process_event(
            session,
            AgentEvent(type="tool.call", data={"tool": "read"}, event_id="legacy-call-1"),
        )
        generated_id = loop.progress.last_action["tool_call_id"]
        restored_progress = ProgressLedger.model_validate_json(
            (tmp_path / "progress.json").read_text(encoding="utf-8")
        )
        assert restored_progress.tool_event_correlations["legacy-call-1"] == generated_id
        loop.progress = restored_progress
        await loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={"tool": "read", "is_error": False},
                event_id="legacy-result-1",
            ),
        )

        assert loop.progress.last_action["tool_call_id"] == generated_id
        assert loop.progress.active_actions == {}
        assert f"action:{generated_id}" in world.snapshot.actions

        await loop.process_event(
            session,
            AgentEvent(type="tool.call", data={"tool": "read"}, event_id="legacy-call-1"),
        )
        assert loop.stats.duplicate_events == 1
        assert loop.progress.tool_calls == 1

    asyncio.run(scenario())


def test_solver_loop_keeps_ambiguous_legacy_result_unresolved(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    loop = SolverLoop(
        world=world,
        progress=ProgressLedger(),
        run_dir=tmp_path,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run-ambiguous", "task-ambiguous"),
        actor="agent:test",
        context_query="ambiguous legacy tool results",
        planned_world_revision=world.snapshot.revision,
    )
    session = FakeSession()

    async def scenario() -> None:
        for event_id in ("legacy-call-a", "legacy-call-b"):
            await loop.process_event(
                session,
                AgentEvent(type="tool.call", data={"tool": "read"}, event_id=event_id),
            )
        await loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={"tool": "read", "is_error": False},
                event_id="legacy-result-ambiguous",
            ),
        )

    asyncio.run(scenario())

    assert len(loop.progress.active_actions) == 2


def test_solver_loop_can_disable_optional_heuristic_planner(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(replan_reasons=["verification_failed"])
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run-no-planner", "task-no-planner"),
        actor="agent:test",
        context_query="planner fallback",
        planned_world_revision=world.snapshot.revision,
        skills=[SkillSpec(id="candidate", description="candidate skill")],
        planner_enabled=False,
    )
    session = FakeSession()

    async def scenario() -> None:
        decision = await loop.process_event(session, AgentEvent(type="other", data={}))
        assert decision.action is SolverAction.CONTINUE
        assert decision.reason == "replan_suppressed"

    asyncio.run(scenario())
    assert not (tmp_path / "plan.json").exists()
    assert progress.replan_count == 0


def test_solver_loop_publishes_initial_and_replan_updates(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-plan", "task-plan")
    session = FakeSession()
    skill = SkillSpec(
        id="alternate-path",
        description="Try an alternate path",
        produces=[{"kind": "observation", "type": "alternate.evidence"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
        plan_horizon=1,
    )

    initial = asyncio.run(loop.maybe_replan(session, force=True))
    assert initial is not None
    assert initial.actions[0].skill_id == "alternate-path"
    assert (tmp_path / "plan.json").is_file()

    progress.record_submission(accepted=False, completed=False)
    replanned = asyncio.run(loop.maybe_replan(session))
    assert replanned is not None
    assert replanned.replan_required is True
    assert "benchmark_negative_feedback" in replanned.replan_reasons
    assert progress.replan_reasons == []

    updates = [item for item in session.feedback if item.type == "solver.plan.updated"]
    assert len(updates) == 2
    assert updates[-1].data["actions"][0]["skill_id"] == "alternate-path"
    assert updates[-1].data["plan_path"] == "plan.json"

    # Repeated identical information does not create an endless plan stream.
    progress.request_replan("benchmark_negative_feedback")
    assert asyncio.run(loop.maybe_replan(session)) is None
    assert progress.replan_reasons == ["benchmark_negative_feedback"]


def test_solver_loop_ignores_world_revisions_that_do_not_change_the_plan(
    tmp_path: Path,
) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-rev", "task-rev")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
        plan_horizon=1,
    )

    assert asyncio.run(loop.maybe_replan(session, force=True)) is not None
    assert progress.replan_count == 1

    # A verifier-style reason recurs on every event and the world revision keeps
    # advancing, but the recommended actions never change. The plan must not be
    # recomputed or re-published for each of them.
    emitted = 0
    for index in range(5):
        world.upsert("observation", {"id": f"noise-{index}", "type": "unrelated.noise"})
        progress.request_replan("expected_observation_missing")
        if asyncio.run(loop.maybe_replan(session)) is not None:
            emitted += 1

    assert emitted <= 1
    assert progress.replan_count <= 2


def test_solver_loop_replans_when_the_frontier_changes(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-frontier", "task-frontier")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
        plan_horizon=1,
    )

    assert asyncio.run(loop.maybe_replan(session, force=True)) is not None
    assert progress.replan_count == 1

    # Satisfying the skill's output removes it from the frontier, so the plan
    # content changes and a new plan must be published.
    world.upsert("observation", {"id": "done", "type": "probe.result"})
    progress.request_replan("world_state_changed")
    replanned = asyncio.run(loop.maybe_replan(session))
    assert replanned is not None
    assert progress.replan_count == 2
    assert replanned.actions == []


def test_solver_loop_uses_typed_trusted_action_verdict(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    verifier_registry = VerifierRegistry()
    tool_authority = verifier_registry.register("tool_adapter")
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-verified", "task-verified")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="verify the service",
        planned_world_revision=world.snapshot.revision,
        verifier_registry=verifier_registry,
    )

    async def run() -> None:
        await loop.process_event(
            session,
            AgentEvent(
                type="action.intent",
                data={"expected_observations": ["service is reachable"]},
            ),
        )
        await loop.process_event(
            session,
            AgentEvent(
                type="tool.call",
                data={"tool": "probe", "tool_call_id": "call-1"},
            ),
        )
        await loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={"tool": "probe", "tool_call_id": "call-1", "is_error": False},
            ),
            trusted_verdict=verifier_registry.authorize_action(
                tool_authority,
                ActionVerdict(
                    run_id="run-verified",
                    action_id="call-1",
                    producer="tool_adapter",
                    captured_at=datetime.now(UTC),
                    world_revision=world.snapshot.revision,
                    execution_status="succeeded",
                    status="verified",
                    actual_observation="service answered the independent probe",
                    evidence=(
                        EvidenceRef(
                            evidence_id="probe-result-1",
                            run_id="run-verified",
                            action_id="call-1",
                            producer="tool_adapter",
                            captured_at=datetime.now(UTC),
                            world_revision=world.snapshot.revision,
                            artifact_ref="artifact://probe-result-1",
                        ),
                    ),
                ),
            ),
        )

    asyncio.run(run())

    assert progress.last_verification["status"] == "verified"
    assert progress.evidence_confirmed == 1
    assert progress.no_progress_count == 0
    assert progress.last_verification["evidence"][0]["evidence_id"] == "probe-result-1"
    assert (
        world.snapshot.observations["verified-action:call-1"].provenance.epistemic_status
        == "verified"
    )


def test_solver_loop_deduplicates_identical_event_ids_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-dedupe", "task-dedupe")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="dedupe events",
        planned_world_revision=world.snapshot.revision,
    )
    event = AgentEvent(
        type="progress.updated",
        data={"confirmed_fact": "the host is reachable"},
        event_id="event-1",
    )

    async def run() -> None:
        await loop.process_event(session, event)
        await loop.process_event(session, event)
        await loop.process_event(
            session,
            AgentEvent(
                type="progress.updated",
                data={"confirmed_fact": "a different claim"},
                event_id="event-1",
            ),
        )

    asyncio.run(run())

    assert progress.claims == ["the host is reachable"]
    assert loop.stats.duplicate_events == 1
    assert loop.stats.conflicting_event_ids == 1


def test_solver_loop_stops_after_bounded_no_progress_replans(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-bounded", "task-bounded")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="make bounded progress",
        planned_world_revision=world.snapshot.revision,
    )

    async def run():
        decision = None
        for index in range(6):
            decision = await loop.process_event(
                session,
                AgentEvent(
                    type="tool.result",
                    event_id=f"result-{index}",
                    data={
                        "tool": "probe",
                        "tool_call_id": f"call-{index}",
                        "is_error": True,
                    },
                ),
            )
        return decision

    decision = asyncio.run(run())

    assert decision.action is SolverAction.STOP
    assert decision.reason == "no_progress_limit"
    assert progress.no_progress_count == 6
    assert progress.replan_count == 5
    assert any(item.type == "solver.stop" for item in session.feedback)
    decisions = [
        item
        for item in trace.path.read_text(encoding="utf-8").splitlines()
        if '"type":"solver.decision"' in item
    ]
    assert '"action":"stop"' in decisions[-1]


def test_solver_loop_preserves_stream_order_and_world_revisions_on_replay(
    tmp_path: Path,
) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-order", "task-order")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="preserve stream order",
        planned_world_revision=world.snapshot.revision,
    )
    first = AgentEvent(
        type="world.observe",
        event_id="stream-2",
        data={"id": "obs-first", "type": "finding", "summary": "first", "content": {}},
    )
    second = AgentEvent(
        type="world.observe",
        event_id="stream-1",
        data={"id": "obs-second", "type": "finding", "summary": "second", "content": {}},
    )

    async def run() -> None:
        await loop.process_event(session, first)
        await loop.process_event(session, second)
        await loop.process_event(session, first)

    asyncio.run(run())

    events = [event for event in world.events() if event.kind == "observation"]
    assert world.snapshot.revision == 2
    assert [event.object["id"] for event in events] == ["obs-first", "obs-second"]
    assert [event.source_event_id for event in events] == ["stream-2", "stream-1"]


def test_solver_loop_backfills_expected_observation_from_the_plan(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-fallback", "task-fallback")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
        plan_horizon=1,
    )

    plan = asyncio.run(loop.maybe_replan(session, force=True))
    assert plan is not None
    assert progress.expected_observation is None

    # The Agent runs a tool without declaring an expectation; the published plan
    # supplies one so action-level verification and its feedback keep working.
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(type="tool.call", data={"tool": "bash", "tool_call_id": "call-1"}),
        )
    )
    assert progress.expected_observation == plan.actions[0].expected_observations[0]

    skipped_before = progress.skipped_verifications
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={
                    "tool": "bash",
                    "tool_call_id": "call-1",
                    "observation": "some tool output",
                },
            ),
        )
    )
    assert progress.skipped_verifications == skipped_before
    assert (progress.last_verification or {}).get("status") == "pending"
    assert any(item.type == "solver.verification" for item in session.feedback)


def test_solver_loop_declared_expectation_wins_over_plan_fallback(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-declared", "task-declared")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
        plan_horizon=1,
    )
    asyncio.run(loop.maybe_replan(session, force=True))

    progress.record_intent({"skill_id": "probe", "expected_observations": ["agent declared this"]})
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(type="tool.call", data={"tool": "bash", "tool_call_id": "call-2"}),
        )
    )
    assert progress.expected_observation == "agent declared this"


def test_solver_loop_steers_once_per_budget_threshold(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"max_tokens": 1000})
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-budget", "task-budget")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=world.snapshot.revision,
        skills=[],
    )

    def usage(tokens: int) -> None:
        asyncio.run(
            loop.process_event(
                session,
                AgentEvent(
                    type="model.usage", data={"total_tokens": tokens, "cost_usd": 0.0}
                ),
            )
        )

    def steers() -> list:
        return [item for item in session.feedback if item.type == "solver.budget"]

    usage(400)  # 60% remaining: above every threshold
    assert steers() == []

    usage(150)  # 45% remaining: crosses 50%
    assert [item.data["threshold"] for item in steers()] == [0.5]

    usage(150)  # 30% remaining: 25% threshold not crossed yet
    assert [item.data["threshold"] for item in steers()] == [0.5]

    usage(100)  # 20% remaining: crosses 25%
    assert [item.data["threshold"] for item in steers()] == [0.5, 0.25]

    usage(100)  # 10% remaining: crosses 10%
    assert [item.data["threshold"] for item in steers()] == [0.5, 0.25, 0.1]

    usage(100)  # already fired: never repeats
    assert [item.data["threshold"] for item in steers()] == [0.5, 0.25, 0.1]
    assert steers()[0].data["tightest_budget"] == "max_tokens"


def test_solver_loop_steers_finalize_once_after_the_soft_deadline(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"wall_time": 60}, wall_time_grace_seconds=180)
    progress.started_at = datetime.now(UTC) - timedelta(seconds=90)
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-finalize", "task-finalize")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=world.snapshot.revision,
        skills=[],
    )
    usage = AgentEvent(type="model.usage", data={"total_tokens": 1, "cost_usd": 0.0})

    asyncio.run(loop.process_event(session, usage))
    finalize = [item for item in session.feedback if item.type == "solver.finalize"]
    assert len(finalize) == 1
    assert finalize[0].data["summary_file"] == "agent.summary.md"
    # The soft deadline does not stop the run: the grace window keeps the budget
    # fraction positive so the Agent can write its report.
    assert progress.remaining_budget_fraction > 0.0

    asyncio.run(loop.process_event(session, usage))
    assert len([item for item in session.feedback if item.type == "solver.finalize"]) == 1


def test_solver_loop_opens_a_round_when_the_graph_changes(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-round", "task-round")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
    )
    asyncio.run(loop.maybe_replan(session, force=True))

    def rounds() -> list:
        return [item for item in session.feedback if item.type == "solver.round"]

    # Nothing has changed yet: no round is opened.
    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert rounds() == []

    # A new evidence-tier fact changes the graph, so the next round is seeded
    # with what the previous one established.
    world.upsert(
        "observation",
        {"id": "obs:found", "type": "web.endpoint"},
        actor="benchmark:demo",
    )
    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert len(rounds()) == 1
    brief = rounds()[0].data["brief"]
    assert "Round 2 brief" in brief
    assert "web.endpoint" in brief
    assert (tmp_path / "round-2.md").is_file()

    # The same graph must not re-open an identical round.
    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert len(rounds()) == 1


def test_solver_loop_opens_a_round_after_a_stall(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-stall", "task-stall")
    session = FakeSession()
    skill = SkillSpec(
        id="probe",
        description="Probe the target",
        produces=[{"kind": "observation", "type": "probe.result"}],
    )
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="probe the target",
        planned_world_revision=world.snapshot.revision,
        skills=[skill],
    )
    asyncio.run(loop.maybe_replan(session, force=True))

    def rounds() -> list:
        return [item for item in session.feedback if item.type == "solver.round"]

    for _ in range(SolverLoop.ROUND_STALL_STEPS - 1):
        asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert rounds() == []

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert len(rounds()) == 1
    assert rounds()[0].data["reason"] == "stall"


def test_solver_loop_reasks_for_the_summary_once_the_agent_settles(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"wall_time": 60}, wall_time_grace_seconds=180)
    progress.started_at = datetime.now(UTC) - timedelta(seconds=90)
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-reask", "task-reask")
    session = FakeSession()
    summary_path = tmp_path / "agent.summary.md"
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=world.snapshot.revision,
        skills=[],
        summary_path=summary_path,
    )
    usage = AgentEvent(type="model.usage", data={"total_tokens": 1, "cost_usd": 0.0})

    asyncio.run(loop.process_event(session, usage))
    assert len([o for o in session.feedback if o.type == "solver.finalize"]) == 1

    # The Agent settles without writing the report: ask again with a fresh turn.
    asyncio.run(loop.process_event(session, AgentEvent(type="pi.agent_settled", data={})))
    finalize = [o for o in session.feedback if o.type == "solver.finalize"]
    assert len(finalize) == 2
    assert finalize[-1].data["reason"] == "summary_missing"

    # Only once, and never when the report already exists.
    asyncio.run(loop.process_event(session, AgentEvent(type="pi.agent_settled", data={})))
    assert len([o for o in session.feedback if o.type == "solver.finalize"]) == 2


def test_solver_loop_does_not_reask_when_the_summary_exists(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"wall_time": 60}, wall_time_grace_seconds=180)
    progress.started_at = datetime.now(UTC) - timedelta(seconds=90)
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-have", "task-have")
    session = FakeSession()
    summary_path = tmp_path / "agent.summary.md"
    summary_path.write_text("done\n", encoding="utf-8")
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=world.snapshot.revision,
        skills=[],
        summary_path=summary_path,
    )

    asyncio.run(
        loop.process_event(session, AgentEvent(type="model.usage", data={"total_tokens": 1}))
    )
    asyncio.run(loop.process_event(session, AgentEvent(type="pi.agent_settled", data={})))
    assert len([o for o in session.feedback if o.type == "solver.finalize"]) == 1
