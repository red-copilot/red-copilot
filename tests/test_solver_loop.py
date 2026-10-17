from __future__ import annotations

import asyncio
import json
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


def test_solver_loop_fetches_hint_once_at_the_threshold(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-hint", "task-hint")
    session = FakeSession()
    fetches: list[int] = []

    async def fetch_hint() -> str | None:
        fetches.append(1)
        return "peek at the upload form"

    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="stuck run",
        planned_world_revision=world.snapshot.revision,
        hint_request=fetch_hint,
        hint_threshold=2,
    )

    async def run() -> None:
        for index in range(3):
            await loop.process_event(
                session,
                AgentEvent(
                    type="tool.result",
                    event_id=f"hint-{index}",
                    data={
                        "tool": f"probe-{index}",
                        "tool_call_id": f"hint-call-{index}",
                        "is_error": True,
                    },
                ),
            )

    asyncio.run(run())

    assert fetches == [1]
    hints = [item for item in session.feedback if item.type == "solver.hint"]
    assert len(hints) == 1
    assert hints[0].data["source"] == "platform"
    assert "peek at the upload form" in hints[0].data["message"]


def test_solver_loop_hint_failure_is_not_retried(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-hint-fail", "task-hint-fail")
    session = FakeSession()
    fetches: list[int] = []

    async def fetch_hint() -> str | None:
        fetches.append(1)
        raise RuntimeError("hint service down")

    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="stuck run",
        planned_world_revision=world.snapshot.revision,
        hint_request=fetch_hint,
        hint_threshold=1,
    )

    async def run() -> None:
        for index in range(2):
            await loop.process_event(
                session,
                AgentEvent(
                    type="tool.result",
                    event_id=f"fail-{index}",
                    data={
                        "tool": f"probe-fail-{index}",
                        "tool_call_id": f"fail-call-{index}",
                        "is_error": True,
                    },
                ),
            )

    asyncio.run(run())

    assert fetches == [1]
    assert not [item for item in session.feedback if item.type == "solver.hint"]
    traces = trace.path.read_text(encoding="utf-8")
    assert '"type":"solver.hint"' in traces
    assert '"used":false' in traces
    assert '"source":"platform"' in traces


def test_operator_and_platform_hints_share_one_steered_event_type() -> None:
    """Both hint sources reach the Agent as ``solver.hint`` observations.

    The RPC session only steers observation types on its whitelist, so the
    shared type has to be there or a hint would stop at ``agent.feedback.jsonl``.
    """
    from harness.pi_container import ContainerPiRpcSession

    assert "solver.hint" in ContainerPiRpcSession._STEER_TYPES


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


def _scored_loop(tmp_path: Path, **kwargs) -> tuple[SolverLoop, ProgressLedger, FakeSession]:
    """A benchmark-shaped loop: wall-clock bound and scored through submissions."""
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"wall_time": 1800}, started_at=_started(900))
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-scored", "task-scored")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
        **kwargs,
    )
    return loop, progress, session


def _started(elapsed_seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=elapsed_seconds)


def test_solver_loop_steers_for_a_missing_submission_once_past_halfway(
    tmp_path: Path,
) -> None:
    loop, _progress, session = _scored_loop(tmp_path, submission_due_fraction=0.5)

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    due = [item for item in session.feedback if item.type == "solver.submission_due"]
    assert len(due) == 1
    assert due[0].data["threshold"] == 0.5
    assert "HARNESS_SUBMISSION_INBOX" in due[0].data["message"]
    assert due[0].data["elapsed_fraction"] > 0.5
    assert loop.metrics_snapshot()["steer_counts"]["submission"] == 1

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))
    assert len([item for item in session.feedback if item.type == "solver.submission_due"]) == 1


def test_solver_loop_skips_the_submission_steer_when_a_candidate_was_sent(
    tmp_path: Path,
) -> None:
    loop, progress, session = _scored_loop(tmp_path, submission_due_fraction=0.5)
    progress.record_submission(accepted=False, completed=False)

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))

    assert [item for item in session.feedback if item.type == "solver.submission_due"] == []


def _finalize_messages(tmp_path: Path, **kwargs) -> list[str]:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"max_tokens": 100})
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-finalize", "task-finalize")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
        **kwargs,
    )
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(type="model.usage", data={"total_tokens": 120, "cost_usd": 0.0}),
        )
    )
    asyncio.run(loop.process_event(session, AgentEvent(type="pi.agent_settled", data={})))
    return [
        item.data["message"]
        for item in session.feedback
        if item.type == "solver.finalize"
    ]


def test_finalize_asks_for_a_best_effort_submission_in_scored_runs(tmp_path: Path) -> None:
    messages = _finalize_messages(tmp_path, finalize_requests_submission=True)

    assert len(messages) == 2  # the soft-limit steer and the settle re-ask
    for message in messages:
        assert "HARNESS_SUBMISSION_INBOX" in message
        assert "HARNESS_SUMMARY_FILE" in message


def test_finalize_leaves_task_runs_without_a_submission_step(tmp_path: Path) -> None:
    messages = _finalize_messages(tmp_path)

    assert len(messages) == 2
    for message in messages:
        assert "HARNESS_SUBMISSION_INBOX" not in message
        assert "HARNESS_SUMMARY_FILE" in message


def _settle_loop(
    tmp_path: Path, **kwargs
) -> tuple[SolverLoop, ProgressLedger, FakeSession]:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(
        budget_limits={"wall_time": 1800},
        started_at=_started(100),
    )
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-settle", "task-settle")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
        finalize_requests_submission=True,
        **kwargs,
    )
    return loop, progress, session


def test_settle_without_submission_asks_for_a_candidate_once(tmp_path: Path) -> None:
    loop, _progress, session = _settle_loop(tmp_path)
    (tmp_path / "agent.summary.md").write_text("summary\n", encoding="utf-8")

    settled = AgentEvent(type="pi.agent_settled", data={})
    asyncio.run(loop.process_event(session, settled))
    asyncio.run(loop.process_event(session, settled))

    reasks = [item for item in session.feedback if item.type == "solver.submission_reask"]
    assert len(reasks) == 1
    assert "HARNESS_SUBMISSION_INBOX" in reasks[0].data["message"]
    assert "no more" in reasks[0].data["message"].lower()
    assert loop.metrics_snapshot()["steer_counts"]["submission"] == 1


def test_settle_without_submission_also_requests_a_missing_summary(
    tmp_path: Path,
) -> None:
    loop, _progress, session = _settle_loop(tmp_path)

    asyncio.run(
        loop.process_event(session, AgentEvent(type="pi.agent_settled", data={}))
    )

    reasks = [item for item in session.feedback if item.type == "solver.submission_reask"]
    assert len(reasks) == 1
    assert "HARNESS_SUMMARY_FILE" in reasks[0].data["message"]


def test_settle_without_submission_does_not_reask_after_a_candidate(
    tmp_path: Path,
) -> None:
    loop, progress, session = _settle_loop(tmp_path)
    progress.record_submission(accepted=False, completed=False)

    asyncio.run(
        loop.process_event(session, AgentEvent(type="pi.agent_settled", data={}))
    )

    assert [item for item in session.feedback if item.type == "solver.submission_reask"] == []


def test_settle_without_submission_stays_off_for_task_runs(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(budget_limits={"wall_time": 1800}, started_at=_started(100))
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-task-settle", "task-settle")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
    )

    asyncio.run(
        loop.process_event(session, AgentEvent(type="pi.agent_settled", data={}))
    )

    assert [item for item in session.feedback if item.type == "solver.submission_reask"] == []


def test_settle_without_submission_stays_off_after_objective_completion(
    tmp_path: Path,
) -> None:
    loop, progress, session = _settle_loop(tmp_path)
    progress.objective_completed = True

    asyncio.run(
        loop.process_event(session, AgentEvent(type="pi.agent_settled", data={}))
    )

    assert [item for item in session.feedback if item.type == "solver.submission_reask"] == []


def test_solver_loop_quiet_mode_does_not_echo_agent_authored_world_changes(
    tmp_path: Path,
) -> None:
    loop, _progress, session = _scored_loop(tmp_path, quiet_world_steers=True)

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="world.observe",
                data={
                    "id": "obs-quiet",
                    "type": "web.endpoint",
                    "summary": "endpoint found",
                    "content": {"path": "/admin"},
                    "confidence": 1.0,
                },
            ),
        )
    )

    assert loop.world.revision > 0
    assert [item for item in session.feedback if item.type == "world.state.updated"] == []


def test_solver_loop_quiet_mode_still_steers_harness_authored_world_changes(
    tmp_path: Path,
) -> None:
    from harness.hints import append_hint

    append_hint(run_dir=tmp_path, text="try the admin panel")
    loop, _progress, session = _scored_loop(tmp_path, quiet_world_steers=True)

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))

    steers = [item for item in session.feedback if item.type == "world.state.updated"]
    assert len(steers) == 1
    assert steers[0].data["revision_after"] > steers[0].data["revision_before"]
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


def _repetition_loop(tmp_path: Path) -> tuple[SolverLoop, ProgressLedger, FakeSession]:
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
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
    )
    return loop, progress, session


def _tool_call(call_id: str, family: str, index: int) -> AgentEvent:
    return AgentEvent(
        type="tool.call",
        event_id=f"evt-{index}",
        data={
            "tool": "bash",
            "tool_call_id": call_id,
            "args_family": family,
            "args_fingerprint": f"fingerprint-{index}",
        },
    )


def _record_failed_tool(
    loop: SolverLoop,
    session: FakeSession,
    *,
    call_id: str,
    family: str,
    index: int,
) -> None:
    asyncio.run(loop.process_event(session, _tool_call(call_id, family, index)))
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                event_id=f"result-{index}",
                data={
                    "tool": "bash",
                    "tool_call_id": call_id,
                    "is_error": True,
                    "exit_code": 1,
                },
            ),
        )
    )


def test_solver_loop_intervenes_once_per_repeated_command_family(tmp_path: Path) -> None:
    loop, progress, session = _repetition_loop(tmp_path)

    for index in range(3):
        _record_failed_tool(
            loop,
            session,
            call_id=f"call-{index}",
            family="bash:curl",
            index=index,
        )

    steers = [item for item in session.feedback if item.type == "solver.repetition_steer"]
    assert len(steers) == 1
    assert steers[0].data["family"] == "bash:curl"
    assert steers[0].data["level"] == 1
    assert progress.steered_tool_families["bash:curl"] == 1

    # A fourth call in the same family does not re-nudge at level 1.
    _record_failed_tool(
        loop,
        session,
        call_id="call-3",
        family="bash:curl",
        index=3,
    )
    assert len([item for item in session.feedback if item.type == "solver.repetition_steer"]) == 1


def test_solver_loop_escalates_a_family_that_keeps_repeating(tmp_path: Path) -> None:
    loop, progress, session = _repetition_loop(tmp_path)

    for index in range(6):
        _record_failed_tool(
            loop,
            session,
            call_id=f"call-{index}",
            family="bash:nmap",
            index=index,
        )

    steers = [item for item in session.feedback if item.type == "solver.repetition_steer"]
    levels = [item.data["level"] for item in steers]
    assert levels == [1, 2]
    assert progress.steered_tool_families["bash:nmap"] == 2

    # Level 2 also fires at most once per family.
    _record_failed_tool(
        loop,
        session,
        call_id="call-6",
        family="bash:nmap",
        index=6,
    )
    assert len([item for item in session.feedback if item.type == "solver.repetition_steer"]) == 2


def test_solver_loop_ignores_family_rotation_and_confirmed_progress(tmp_path: Path) -> None:
    loop, progress, session = _repetition_loop(tmp_path)

    for index, family in enumerate(("bash:curl", "bash:nmap", "bash:curl", "bash:nmap")):
        asyncio.run(loop.process_event(session, _tool_call(f"call-{index}", family, index)))

    assert progress.tool_family_streak == 1
    assert [item for item in session.feedback if item.type == "solver.repetition_steer"] == []

    # Confirmed progress clears the streak before the next call.
    asyncio.run(loop.process_event(session, _tool_call("call-4", "bash:curl", 4)))
    asyncio.run(loop.process_event(session, _tool_call("call-5", "bash:curl", 5)))
    assert progress.tool_family_streak == 2
    progress.record_submission(accepted=True, completed=False)

    assert progress.tool_family_streak == 0
    assert progress.last_tool_family is None
    asyncio.run(loop.process_event(session, _tool_call("call-6", "bash:curl", 6)))
    assert [item for item in session.feedback if item.type == "solver.repetition_steer"] == []


def test_context_restart_waits_for_new_work_after_a_restart() -> None:
    import harness.progress as progress_module

    limit = 10.0
    window = int(limit * progress_module.CONTEXT_RESTART_BUDGET_FRACTION)
    progress = ProgressLedger(budget_limits={"max_tool_calls": limit})

    for index in range(window):
        progress.record_event(_tool_call(f"call-{index}", "bash:curl", index))
    assert progress.context_restart_due() == "tool_call_budget"

    progress.record_context_restart()

    # The threshold that was just crossed must not re-fire on the next event;
    # the second restart needs a whole new budget window of calls.
    assert progress.context_restart_due() is None
    for index in range(window, 2 * window):
        progress.record_event(_tool_call(f"call-{index}", f"bash:cmd{index}", index))
    assert progress.context_restart_due() == "tool_call_budget"


def test_context_restart_uses_the_tool_call_count_trigger() -> None:
    import harness.progress as progress_module

    threshold = progress_module.CONTEXT_RESTART_TOOL_CALLS
    progress = ProgressLedger(budget_limits={"wall_time": 3600.0})

    for index in range(threshold - 1):
        progress.record_event(_tool_call(f"call-{index}", f"bash:cmd{index}", index))
    assert progress.context_restart_due() is None

    progress.record_event(_tool_call("call-last", "bash:cmd-last", threshold))
    assert progress.context_restart_due() == "tool_calls"


def _metrics_loop(tmp_path: Path, **kwargs) -> tuple[SolverLoop, ProgressLedger, FakeSession]:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger(**kwargs.pop("progress_kwargs", {}))
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-metrics", "task-metrics")
    session = FakeSession()
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        trace=trace,
        actor="agent:test",
        context_query="find the flag",
        planned_world_revision=world.snapshot.revision,
        **kwargs,
    )
    return loop, progress, session


def test_metrics_snapshot_reports_tool_cost_and_final_context(tmp_path: Path) -> None:
    loop, _progress, session = _metrics_loop(tmp_path)

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={
                    "tool": "bash",
                    "tool_call_id": "call-slow",
                    "duration_ms": 90000,
                    "is_error": False,
                    "exit_code": 0,
                },
            ),
        )
    )
    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="model.usage",
                data={
                    "input_tokens": 3000,
                    "cache_read_tokens": 10000,
                    "total_tokens": 13100,
                    "cost_usd": 0.0,
                },
            ),
        )
    )

    metrics = loop.metrics_snapshot()
    assert metrics["tool_calls"] == 1
    assert metrics["tool_failures"] == 0
    assert metrics["tool_time_ms"] == 90000
    assert metrics["context_tokens_final"] == 13000
    assert metrics["slow_tool_calls"] == [
        {
            "tool": "bash",
            "tool_call_id": "call-slow",
            "duration_ms": 90000,
            "is_error": False,
        }
    ]


def test_metrics_snapshot_counts_only_delivered_steers(tmp_path: Path) -> None:
    """A quieted world echo is a trace event, not a steer the Agent received."""
    loop, _progress, session = _metrics_loop(tmp_path, quiet_world_steers=True)

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="world.observe",
                data={
                    "id": "obs-metrics",
                    "type": "web.endpoint",
                    "summary": "endpoint found",
                    "content": {"path": "/admin"},
                    "confidence": 1.0,
                },
            ),
        )
    )

    assert [item for item in session.feedback if item.type == "world.state.updated"] == []
    assert loop.metrics_snapshot()["steer_counts"]["world"] == 0


def test_metrics_snapshot_counts_a_harness_authored_world_steer(tmp_path: Path) -> None:
    from harness.hints import append_hint

    append_hint(run_dir=tmp_path, text="try the admin panel")
    loop, _progress, session = _metrics_loop(tmp_path, quiet_world_steers=True)

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))

    assert len([item for item in session.feedback if item.type == "world.state.updated"]) == 1
    assert loop.metrics_snapshot()["steer_counts"]["world"] == 1


def test_metrics_snapshot_counts_the_finalize_steer_once(tmp_path: Path) -> None:
    loop, _progress, session = _metrics_loop(
        tmp_path,
        progress_kwargs={"budget_limits": {"max_tokens": 100}, "wall_time_grace_seconds": 180},
    )
    usage = AgentEvent(type="model.usage", data={"total_tokens": 120, "cost_usd": 0.0})

    asyncio.run(loop.process_event(session, usage))
    asyncio.run(loop.process_event(session, usage))

    assert len([item for item in session.feedback if item.type == "solver.finalize"]) == 1
    assert loop.metrics_snapshot()["steer_counts"]["finalize"] == 1


def test_written_context_carries_the_remaining_window(tmp_path: Path) -> None:
    loop, _progress, session = _metrics_loop(
        tmp_path,
        progress_kwargs={"budget_limits": {"wall_time": 600, "max_tokens": 1000}},
    )

    asyncio.run(loop.process_event(session, AgentEvent(type="tool.result", data={})))

    context = (tmp_path / "world.context.txt").read_text(encoding="utf-8")
    assert "## Focus" in context
    assert "budget: wall_time 100% left" in context
    # A dimension appears only once it has been charged; the wall clock is
    # always visible because it is the window every decision is paced by.
    assert "max_tokens" not in context

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(type="model.usage", data={"total_tokens": 250, "cost_usd": 0.0}),
        )
    )

    context = (tmp_path / "world.context.txt").read_text(encoding="utf-8")
    assert "budget: max_tokens 75% left" in context


def test_a_blocking_command_is_traced_and_surfaced_once(tmp_path: Path) -> None:
    """One 927s call ate half a window in the 2026-10-16 sweep, silently."""
    loop, _progress, session = _metrics_loop(tmp_path)
    slow = AgentEvent(
        type="tool.result",
        data={
            "tool": "bash",
            "tool_call_id": "call-blocking",
            "duration_ms": 814_000,
            "is_error": False,
            "exit_code": 0,
        },
    )

    asyncio.run(loop.process_event(session, slow))
    asyncio.run(loop.process_event(session, slow))

    trace_types = [
        json.loads(line)["type"]
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert trace_types.count("tool.slow") == 2
    slow_steers = [item for item in session.feedback if item.type == "solver.tool_slow"]
    assert len(slow_steers) == 1
    assert "814s" in slow_steers[0].data["message"]
    assert loop.metrics_snapshot()["tool_time_ms"] == 1_628_000


def test_a_bounded_command_is_not_flagged(tmp_path: Path) -> None:
    loop, _progress, session = _metrics_loop(tmp_path)

    asyncio.run(
        loop.process_event(
            session,
            AgentEvent(
                type="tool.result",
                data={
                    "tool": "bash",
                    "tool_call_id": "call-bounded",
                    "duration_ms": 4_000,
                    "is_error": False,
                    "exit_code": 0,
                },
            ),
        )
    )

    trace = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert "tool.slow" not in trace
    assert [item for item in session.feedback if item.type == "solver.tool_slow"] == []


def test_closure_switches_default_off_for_task_runs(tmp_path: Path) -> None:
    """A local task run must not inherit the benchmark closure behaviour."""
    loop, _progress, _session = _metrics_loop(tmp_path)

    assert loop.finalize_requests_submission is False
    assert loop.submission_due_fraction is None
    assert loop.quiet_world_steers is False
    assert loop.verdict_steers_only is False
