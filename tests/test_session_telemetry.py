from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import replace as dataclasses_replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent import AgentResult, AgentRunContext, CLIAdapter
from harness.budget import UsageMetrics
from harness.pi_container import ContainerPiRpcSession, ContainerPiSession
from harness.progress import ProgressLedger
from harness.session import (
    ONE_SHOT_AGENT_CAPABILITIES,
    RPC_AGENT_CAPABILITIES,
    AgentEvent,
    AgentObservation,
    OneShotAgentSession,
    SessionPhase,
    SessionPhaseTracker,
)
from harness.trace import TraceRecorder
from harness.verification import EvidenceRef, ObjectiveVerdict, VerifierRegistry


def _run_context(run_dir: Path, **overrides) -> AgentRunContext:
    """Minimal AgentRunContext for session-level tests (adapters never read task)."""
    fields = {"task": object(), "task_dir": run_dir, "run_dir": run_dir, "seed": 1}
    fields.update(overrides)
    return AgentRunContext(**fields)


class FakeStreamingAdapter:
    def run(self, context: AgentRunContext, **_kwargs) -> AgentResult:
        run_dir = Path(context.run_dir)
        event_path = run_dir / "events.jsonl"
        event_path.write_text("", encoding="utf-8")
        for index in range(2):
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "progress.updated",
                            "data": {"step": index + 1},
                        }
                    )
                    + "\n"
                )
                handle.flush()
            time.sleep(0.05)
        return AgentResult(
            returncode=0,
            timed_out=False,
            budget_exceeded=None,
            stdout="",
            stderr="",
            metrics=UsageMetrics(),
        )


def test_trace_emits_causal_identifiers(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test")
    parent = recorder.emit("plan.created", plan_id="plan-1")
    child = recorder.emit(
        "action.started",
        parent_event_id=parent,
        plan_id="plan-1",
        action_id="action-1",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["event_id"] == parent
    assert rows[1]["event_id"] == child
    assert rows[1]["parent_event_id"] == parent
    assert rows[1]["plan_id"] == "plan-1"
    assert rows[1]["action_id"] == "action-1"


def test_oneshot_agent_session_streams_events_and_feedback(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = await OneShotAgentSession.start(
            FakeStreamingAdapter(),
            context=_run_context(tmp_path),
        )
        events = [event async for event in session.events()]
        assert [event.data["step"] for event in events] == [1, 2]
        assert [event.event_id for event in events] == [
            "agent-event-line:1",
            "agent-event-line:2",
        ]

        await session.observe(
            AgentObservation(
                type="benchmark.feedback",
                data={"accepted": True, "progress": 1},
            )
        )
        checkpoint = await session.checkpoint()
        assert checkpoint.event_offset > 0
        assert checkpoint.feedback_offset > 0

        await session.close("objective-complete")
        result = await session.result()
        assert result.returncode == 0

        feedback = (tmp_path / "agent.feedback.jsonl").read_text(encoding="utf-8")
        assert "benchmark.feedback" in feedback
        control = (tmp_path / "agent.control.jsonl").read_text(encoding="utf-8")
        assert "objective-complete" in control

    asyncio.run(scenario())


def test_oneshot_close_terminates_running_cli_process(tmp_path: Path) -> None:
    from harness.models import AgentSpec, load_task

    async def scenario() -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        spec = AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "long-running-cli",
                "type": "cli",
                "command": ["python", "-c", "import time; time.sleep(60)"],
            }
        )
        adapter = CLIAdapter(
            spec,
            allow_host=True,
            trace=TraceRecorder(run_dir / "trace.jsonl", "run", "hello-001"),
        )
        task_path = Path("benchmarks/examples/hello/task.yaml").resolve()
        session = await adapter.start_session(
            AgentRunContext(
                task=load_task(task_path),
                task_dir=task_path.parent,
                run_dir=run_dir,
                seed=1,
            )
        )
        try:
            await asyncio.sleep(0.2)
            await session.close("test-interrupt")
            result = await asyncio.wait_for(session.result(), timeout=3)
            assert result.returncode != 0
        finally:
            await session.close("test-cleanup")

    asyncio.run(scenario())


def test_oneshot_session_tracks_live_budget_usage(tmp_path: Path) -> None:
    session = OneShotAgentSession(object(), context=_run_context(tmp_path))
    session._account(AgentEvent(type="model.request", data={}))
    session._account(
        AgentEvent(
            type="model.usage",
            data={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17, "cost_usd": 0.03},
        )
    )
    session._account(AgentEvent(type="tool.call", data={}))

    assert session.usage_metrics == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "model_calls": 1,
        "tool_calls": 1,
        "cost_usd": 0.03,
    }


@pytest.mark.parametrize(
    "event",
    [
        AgentEvent(type="model.usage_missing", data={"source": "pi"}),
        AgentEvent(type="model.usage", data={"input_tokens": "many"}),
        AgentEvent(type="model.usage", data={"total_tokens": -1}),
        AgentEvent(
            type="model.usage",
            data={"input_tokens": 6, "output_tokens": 5, "total_tokens": 1},
        ),
        AgentEvent(type="model.usage", data={"cost_usd": float("nan")}),
        AgentEvent(type="model.request", data={"count": True}),
        AgentEvent(type="tool.call", data={"count": -2}),
    ],
)
def test_oneshot_session_rejects_invalid_usage_without_corrupting_checkpoint_metrics(
    tmp_path: Path, event: AgentEvent
) -> None:
    session = OneShotAgentSession(object(), context=_run_context(tmp_path))

    session._account(event)

    assert session.usage_metrics == UsageMetrics().as_dict()
    assert session.usage_telemetry_valid is False


def test_oneshot_checkpoint_cursor_stops_at_last_yielded_event(tmp_path: Path) -> None:
    class BatchedAdapter:
        def run(self, context: AgentRunContext, **_kwargs) -> AgentResult:
            lines = [
                json.dumps({"type": "progress.updated", "data": {"step": step}}) + "\n"
                for step in (1, 2, 3)
            ]
            (Path(context.run_dir) / "events.jsonl").write_text(
                "".join(lines), encoding="utf-8"
            )
            return AgentResult(
                returncode=0,
                timed_out=False,
                budget_exceeded=None,
                stdout="",
                stderr="",
                metrics=UsageMetrics(),
            )

    async def scenario() -> None:
        session = await OneShotAgentSession.start(
            BatchedAdapter(), context=_run_context(tmp_path)
        )
        stream = session.events()
        first = await anext(stream)
        first_checkpoint = await session.checkpoint()
        second = await anext(stream)
        second_checkpoint = await session.checkpoint()

        raw_lines = (tmp_path / "events.jsonl").read_bytes().splitlines(keepends=True)
        assert first.data["step"] == 1
        assert second.data["step"] == 2
        assert first_checkpoint.event_offset == len(raw_lines[0])
        assert second_checkpoint.event_offset == len(raw_lines[0]) + len(raw_lines[1])
        await stream.aclose()

    asyncio.run(scenario())


def test_oneshot_session_refuses_symlinked_agent_event_log(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"model.usage","data":{"total_tokens":999}}\n')
    session = OneShotAgentSession(object(), context=_run_context(tmp_path))
    session.event_path.symlink_to(outside)

    async def scenario() -> None:
        session._task = asyncio.create_task(asyncio.sleep(0))
        await session._task
        with pytest.raises(OSError):
            await anext(session.events())
        assert session.usage_metrics["total_tokens"] == 0

    asyncio.run(scenario())


def test_progress_ledger_tracks_actions_and_submissions(tmp_path: Path) -> None:
    ledger = ProgressLedger(active_goal="goal:test")
    ledger.record_event(
        AgentEvent(
            type="tool.call",
            data={"tool": "bash", "tool_call_id": "1"},
        )
    )
    assert ledger.last_action == {
        "tool": "bash",
        "tool_call_id": "1",
        "status": "running",
        "skill_id": None,
    }

    ledger.record_event(
        AgentEvent(
            type="tool.result",
            data={"tool": "bash", "tool_call_id": "1", "is_error": True},
        )
    )
    assert ledger.failure_count == 1
    assert ledger.no_progress_count == 1

    ledger.record_event(
        AgentEvent(
            type="progress.updated",
            data={
                "current_subgoal": "enumerate web routes",
                "completed_subgoal": "identify service",
                "confirmed_fact": "http is reachable",
                "expected_observation": "admin route exists",
                "actual_observation": "/admin returned 200",
                "hypothesis": {
                    "id": "hyp-admin",
                    "statement": "admin endpoint is exposed",
                    "status": "supported",
                    "confidence": 0.9,
                    "evidence_for": ["obs-admin"],
                    "evidence_against": [],
                },
                "replan_reason": "new capability",
                "made_progress": True,
                "objective_completed": True,
            },
        )
    )
    assert ledger.current_subgoal == "enumerate web routes"
    assert ledger.completed_subgoals == []
    assert ledger.claimed_completed_subgoals == ["identify service"]
    assert ledger.claims == ["http is reachable"]
    assert ledger.completion_claims == ["goal:test"]
    assert ledger.objective_completed is False
    assert ledger.confirmed_facts == []
    assert ledger.execution_succeeded == 0
    assert ledger.no_progress_count == 1
    assert ledger.expected_observation == "admin route exists"
    assert ledger.actual_observation == "/admin returned 200"
    assert ledger.hypotheses["hyp-admin"].status == "supported"

    ledger.record_submission(accepted=True, completed=True)
    assert ledger.accepted_submissions == 1
    assert ledger.objective_completed is False
    assert ledger.no_progress_count == 0
    assert ledger.unconfirmed_progress == 0
    registry = VerifierRegistry()
    authority = registry.register("task_verifier")
    captured_at = datetime.now(UTC)
    verdict = ObjectiveVerdict(
        run_id="run-test",
        producer="task_verifier",
        captured_at=captured_at,
        world_revision=1,
        status="verified",
        evidence=(
            EvidenceRef(
                evidence_id="evidence-test",
                run_id="run-test",
                action_id="objective-evaluation:run-test",
                producer="task_verifier",
                captured_at=captured_at,
                world_revision=1,
                artifact_ref="evidence/sha256/" + "a" * 64 + ".json",
                artifact_hash="a" * 64,
            ),
        ),
    )
    ledger.record_objective_verdict(registry.authorize_objective(authority, verdict), registry)
    assert ledger.objective_completed is True
    assert ledger.no_progress_count == 0

    path = tmp_path / "progress.json"
    ledger.write(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["active_goal"] == "goal:test"
    assert saved["objective_completed"] is True


def test_successful_tool_exit_is_not_task_progress() -> None:
    ledger = ProgressLedger()
    ledger.record_event(
        AgentEvent(
            type="tool.call",
            data={"tool": "bash", "tool_call_id": "call-ok"},
        )
    )
    ledger.record_event(
        AgentEvent(
            type="tool.result",
            data={"tool": "bash", "tool_call_id": "call-ok", "is_error": False},
        )
    )

    assert ledger.execution_succeeded == 1
    assert ledger.no_progress_count == 0
    assert ledger.unconfirmed_progress == 1
    assert ledger.objective_completed is False


def test_container_pi_session_close_kills_container(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("harness.pi_container.subprocess.run", fake_run)
    run_dir = tmp_path / "run_demo"
    run_dir.mkdir()
    session = ContainerPiSession(
        object(),
        context=_run_context(run_dir),
    )
    asyncio.run(session.close("objective-complete"))

    from harness.pi_container import _container_name

    assert calls == [["docker", "kill", _container_name(run_dir)]]
    control = tmp_path / "run_demo" / "agent.control.jsonl"
    assert "objective-complete" in control.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "event",
    [
        AgentEvent(type="model.usage_missing", data={"source": "pi"}),
        AgentEvent(type="model.usage", data={"input_tokens": "many"}),
        AgentEvent(type="model.usage", data={"total_tokens": -1}),
        AgentEvent(
            type="model.usage",
            data={"input_tokens": 6, "output_tokens": 5, "total_tokens": 1},
        ),
        AgentEvent(type="model.usage", data={"cost_usd": float("nan")}),
        AgentEvent(type="model.usage", data={"cost_usd": -1.0}),
        AgentEvent(type="model.usage", data={"model_calls": True}),
        AgentEvent(type="model.request", data={"count": True}),
        AgentEvent(type="tool.call", data={"count": -2}),
    ],
)
def test_usage_accounting_rules_match_between_oneshot_and_pi_sessions(
    tmp_path: Path, event: AgentEvent
) -> None:
    oneshot = OneShotAgentSession(object(), context=_run_context(tmp_path))
    oneshot._account(event)

    pi_session = object.__new__(ContainerPiRpcSession)
    pi_session.adapter = SimpleNamespace(
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test")
    )
    pi_session._metrics = UsageMetrics()
    pi_session._budget_exceeded = None
    pi_session._telemetry_invalid = None
    pi_session._account(event)

    assert oneshot.usage_metrics == UsageMetrics().as_dict()
    assert oneshot.usage_telemetry_valid is False
    assert pi_session._metrics.as_dict() == UsageMetrics().as_dict()
    assert pi_session._budget_exceeded is None
    assert pi_session._telemetry_invalid is not None


def test_session_phase_tracker_records_valid_transitions_in_trace(tmp_path: Path) -> None:
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test")
    tracker = SessionPhaseTracker(trace=trace)

    assert tracker.phase is SessionPhase.CREATED
    tracker.transition(SessionPhase.STARTED)
    tracker.transition(SessionPhase.PROMPTING)
    tracker.transition(SessionPhase.SETTLED)
    tracker.transition(SessionPhase.PROMPTING, cause="steer_prompt")
    tracker.transition(SessionPhase.CLOSED, reason="objective-complete")

    rows = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    phases = [row["data"] for row in rows if row["type"] == "agent.session.phase"]
    assert [(item["from"], item["to"]) for item in phases] == [
        ("created", "started"),
        ("started", "prompting"),
        ("prompting", "settled"),
        ("settled", "prompting"),
        ("prompting", "closed"),
    ]
    assert phases[-1]["reason"] == "objective-complete"
    assert phases[3]["cause"] == "steer_prompt"


def test_session_phase_tracker_rejects_invalid_and_ignores_repeated_transitions() -> None:
    tracker = SessionPhaseTracker()

    with pytest.raises(ValueError):
        tracker.transition(SessionPhase.SETTLED)  # created -> settled skips startup

    tracker.transition(SessionPhase.STARTED)
    tracker.transition(SessionPhase.CLOSED)
    with pytest.raises(ValueError):
        tracker.transition(SessionPhase.PROMPTING)  # terminal phase

    restarted = SessionPhaseTracker()
    restarted.transition(SessionPhase.STARTED)
    assert restarted.transition(SessionPhase.STARTED) is SessionPhase.STARTED  # no-op


def test_agent_capabilities_declare_the_steer_degradation_explicitly() -> None:
    assert ONE_SHOT_AGENT_CAPABILITIES.live_steer is False
    assert RPC_AGENT_CAPABILITIES.live_steer is True
    assert ONE_SHOT_AGENT_CAPABILITIES.submission_channels == ("submission_inbox",)
    assert RPC_AGENT_CAPABILITIES.submission_channels == ("submission_inbox",)
    assert not hasattr(ONE_SHOT_AGENT_CAPABILITIES, "typed_event_channel")
    assert not hasattr(RPC_AGENT_CAPABILITIES, "typed_event_channel")


def test_agent_run_context_is_frozen_and_derived_by_replace(tmp_path: Path) -> None:
    context = AgentRunContext(
        task=object(),
        task_dir=tmp_path,
        run_dir=tmp_path,
        seed=7,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.seed = 8  # type: ignore[misc]

    derived = dataclasses_replace(context, gateway_url="http://gateway", gateway_token="t")
    assert derived.gateway_enabled is True
    assert context.gateway_enabled is False  # the original is untouched
    assert derived.seed == 7


def test_oneshot_session_exposes_capabilities_and_phase_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = await OneShotAgentSession.start(
            FakeStreamingAdapter(), context=_run_context(tmp_path)
        )
        assert session.capabilities == ONE_SHOT_AGENT_CAPABILITIES
        assert session.phase.phase is SessionPhase.STARTED
        await session.close("objective-complete")
        assert session.phase.phase is SessionPhase.CLOSED

    asyncio.run(scenario())
