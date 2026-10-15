from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.progress import ProgressLedger
from harness.session import AgentEvent
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


def test_solver_loop_stops_after_the_bounded_no_progress_limit(tmp_path: Path) -> None:
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
    assert any(item.type == "solver.stop" for item in session.feedback)
    decisions = [
        item
        for item in trace.path.read_text(encoding="utf-8").splitlines()
        if '"type":"solver.decision"' in item
    ]
    assert '"action":"stop"' in decisions[-1]


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
def test_solver_loop_finalize_on_usage_exhaustion_holds_stop_in_grace(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"max_tokens": 100}, wall_time_grace_seconds=180)
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
    )
    usage = AgentEvent(type="model.usage", data={"total_tokens": 120, "cost_usd": 0.0})

    decision = asyncio.run(loop.process_event(session, usage))
    finalize = [item for item in session.feedback if item.type == "solver.finalize"]
    assert len(finalize) == 1
    assert finalize[0].data["reason"] == "soft_budget_limit"
    assert finalize[0].data["budget"] == "max_tokens"
    assert progress.budget_exceeded_at is not None
    # The policy stop for an exhausted budget is held while the grace window
    # is open, so the summary steer is not immediately overridden by a stop.
    assert decision.action is SolverAction.CONTINUE
    assert decision.reason == "budget_grace_window"

    decision = asyncio.run(loop.process_event(session, usage))
    assert decision.action is SolverAction.CONTINUE
    assert decision.reason == "budget_grace_window"
    assert len([item for item in session.feedback if item.type == "solver.finalize"]) == 1

    # Once the grace window has elapsed the budget stop goes through.
    progress.budget_exceeded_at -= timedelta(seconds=181)
    decision = asyncio.run(loop.process_event(session, usage))
    assert decision.action is SolverAction.STOP
    assert decision.reason == "budget_exhausted"
