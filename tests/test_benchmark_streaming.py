from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.agent import AgentResult
from harness.audit import audit_run
from harness.benchmark.base import (
    BenchmarkCase,
    BenchmarkSession,
    EvaluationResult,
    SubmissionResult,
)
from harness.benchmark.runner import BenchmarkRunner
from harness.budget import UsageMetrics
from harness.models import AgentSpec, BudgetSpec, ObjectiveSpec
from harness.session import AgentCheckpoint, AgentEvent


class FakeBenchmarkAdapter:
    def __init__(self) -> None:
        self.submissions: list[str] = []
        self.closed = False
        self.teardown_calls = 0

    async def provision(self, case: BenchmarkCase) -> BenchmarkSession:
        return BenchmarkSession(
            id="session-1",
            benchmark=case.benchmark,
            case_id=case.id,
            objective=ObjectiveSpec(description="submit one flag"),
        )

    async def submit(self, _session, submission):
        self.submissions.append(submission.value)
        return SubmissionResult(
            accepted=True,
            score_delta=10,
            completed=True,
            metadata={"correct_flag_count": 1, "total_flag_count": 1},
        )

    async def evaluate(self, _session):
        return EvaluationResult(success=True, score=10)

    async def teardown(self, _session):
        self.closed = True
        self.teardown_calls += 1


def test_invalid_container_network_is_rejected_before_benchmark_provision(tmp_path: Path) -> None:
    class ProvisionProbe(FakeBenchmarkAdapter):
        provisioned = False

        async def provision(self, case: BenchmarkCase) -> BenchmarkSession:
            self.provisioned = True
            return await super().provision(case)

    adapter = ProvisionProbe()
    agent = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "container",
            "type": "docker",
            "image": "probe",
            "network": "environment",
        }
    )

    with pytest.raises(ValueError, match="do not provide a Harness environment network"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=adapter,
                case=BenchmarkCase(id="CASE-NETWORK", benchmark="fake"),
                agent=agent,
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
            )
        )

    assert adapter.provisioned is False
    assert adapter.closed is False
    assert adapter.teardown_calls == 0


def test_benchmark_tears_down_provisioned_session_when_solver_bootstrap_fails(
    monkeypatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    adapter = FakeBenchmarkAdapter()

    def fail_bootstrap(**_kwargs):
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(runner_module, "bootstrap_solver", fail_bootstrap)

    with pytest.raises(RuntimeError, match="injected bootstrap failure"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=adapter,
                case=BenchmarkCase(id="CASE-BOOTSTRAP", benchmark="fake"),
                agent=AgentSpec.model_validate(
                    {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
                ),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
                allow_host_agent=True,
            )
        )

    assert adapter.closed is True
    assert adapter.teardown_calls == 1
def test_benchmark_provision_error_does_not_persist_exception_details(tmp_path: Path) -> None:
    class FailingProvision(FakeBenchmarkAdapter):
        async def provision(self, _case):
            raise RuntimeError("sensitive opaque provider payload provision-secret-canary")

    with pytest.raises(RuntimeError, match="provision-secret-canary"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=FailingProvision(),
                case=BenchmarkCase(id="CASE-PROVISION-ERROR", benchmark="fake"),
                agent=AgentSpec.model_validate(
                    {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
                ),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
                allow_host_agent=True,
            )
        )

    trace = next(tmp_path.glob("fake_CASE-PROVISION-ERROR_*/trace.jsonl"))
    assert "RuntimeError" in trace.read_text(encoding="utf-8")
    assert "provision-secret-canary" not in trace.read_text(encoding="utf-8")


def test_benchmark_teardown_error_does_not_persist_exception_details(
    monkeypatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    class FailingTeardown(FakeBenchmarkAdapter):
        async def teardown(self, _session):
            raise RuntimeError("sensitive opaque provider payload teardown-secret-canary")

    fake_agent = FakeAgentAdapter()
    monkeypatch.setattr(runner_module, "build_agent_adapter", lambda *_args, **_kwargs: fake_agent)
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FailingTeardown(),
            case=BenchmarkCase(id="CASE-TEARDOWN-ERROR", benchmark="fake"),
            agent=AgentSpec.model_validate(
                {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
            ),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
            allow_host_agent=True,
        )
    )

    run_dir = tmp_path / result["run_id"]
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in run_dir.glob("*.json*"))
    artifacts += (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert result["cleanup"]["ok"] is False
    assert "RuntimeError" in artifacts
    assert "teardown-secret-canary" not in artifacts
def test_benchmark_teardown_finishes_when_run_is_cancelled_during_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    class BlockingTeardown(FakeBenchmarkAdapter):
        def __init__(self):
            super().__init__()
            self.teardown_started = asyncio.Event()
            self.allow_teardown = asyncio.Event()

        async def teardown(self, _session):
            self.teardown_calls += 1
            self.teardown_started.set()
            await self.allow_teardown.wait()
            self.closed = True

    async def scenario() -> None:
        fake_agent = FakeAgentAdapter()
        monkeypatch.setattr(
            runner_module, "build_agent_adapter", lambda *_args, **_kwargs: fake_agent
        )
        benchmark = BlockingTeardown()
        run_task = asyncio.create_task(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=benchmark,
                case=BenchmarkCase(id="CASE-CANCEL-TEARDOWN", benchmark="fake"),
                agent=AgentSpec.model_validate(
                    {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
                ),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
                allow_host_agent=True,
            )
        )
        await asyncio.wait_for(benchmark.teardown_started.wait(), timeout=5)
        run_task.cancel()
        await asyncio.sleep(0)
        assert not run_task.done()
        benchmark.allow_teardown.set()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert benchmark.closed is True
        assert benchmark.teardown_calls == 1

    asyncio.run(scenario())


def test_benchmark_teardown_timeout_cancels_stuck_cleanup(monkeypatch, tmp_path: Path) -> None:
    import harness.benchmark.runner as runner_module

    class StuckTeardown(FakeBenchmarkAdapter):
        def __init__(self):
            super().__init__()
            self.teardown_cancelled = False

        async def teardown(self, _session):
            try:
                await asyncio.Event().wait()
            finally:
                self.teardown_cancelled = True

    fake_agent = FakeAgentAdapter()
    benchmark = StuckTeardown()
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: fake_agent
    )
    monkeypatch.setattr(runner_module, "BENCHMARK_TEARDOWN_TIMEOUT_SECONDS", 0.01)
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=BenchmarkCase(id="CASE-TEARDOWN-TIMEOUT", benchmark="fake"),
            agent=AgentSpec.model_validate(
                {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
            ),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
            allow_host_agent=True,
        )
    )

    assert benchmark.teardown_cancelled is True
    assert result["cleanup"]["ok"] is False
    assert result["cleanup"]["error"] == "TimeoutError: details omitted"
class FakeSession:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.feedback = []
        self.closed_reason = None
        self.checkpoint_calls = 0

    async def events(self):
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="action.intent",
            data={
                "action_id": "action-probe-admin",
                "description": "probe admin endpoint",
                "expected_observations": ["admin endpoint exists"],
                "success_conditions": ["HTTP response proves /admin is routed"],
                "replan_conditions": ["endpoint absent"],
            },
        )
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="world.observe",
            data={
                "id": "obs-live",
                "type": "web.endpoint",
                "summary": "admin endpoint exists at /admin",
                "content": {"path": "/admin", "status": 200},
                "confidence": 1.0,
            },
        )
        path = self.run_dir / "submission.inbox.jsonl"
        path.write_text(
            json.dumps({"type": "flag", "value": "flag{live}"}) + "\n",
            encoding="utf-8",
        )
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="progress.updated",
            data={"step": 1, "made_progress": True},
        )

    async def checkpoint(self):
        return AgentCheckpoint(
            id=f"checkpoint-{self.checkpoint_calls}",
            event_offset=self.checkpoint_calls,
            feedback_offset=0,
        )

    async def observe(self, observation):
        self.feedback.append(observation)

    async def close(self, reason: str):
        self.closed_reason = reason

    async def result(self):
        return AgentResult(
            returncode=0,
            timed_out=False,
            budget_exceeded=None,
            stdout="",
            stderr="",
            metrics=UsageMetrics(),
        )


class FakeAgentAdapter:
    def __init__(self) -> None:
        self.session: FakeSession | None = None

    async def start_session(self, context, **_kwargs):
        self.session = FakeSession(context.run_dir)
        return self.session


class CrashingFakeSession(FakeSession):
    async def events(self):
        assert (self.run_dir.parent / "checkpoint.json").is_file()
        raise RuntimeError("injected agent session crash")
        yield  # Make this an async generator.


class CrashingFakeAgentAdapter(FakeAgentAdapter):
    async def start_session(self, context, **_kwargs):
        self.session = CrashingFakeSession(context.run_dir)
        return self.session


def _run_case_with_agent(monkeypatch, tmp_path: Path, agent_adapter, budgets, *, adapter=None):
    import harness.benchmark.runner as runner_module

    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: agent_adapter,
    )
    agent = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "demo",
            "type": "cli",
            "command": ["true"],
        }
    )
    return asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=adapter if adapter is not None else FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-BUDGET", benchmark="fake"),
            agent=agent,
            budgets=budgets,
            seed=1,
            submission_extractor=lambda _result: [],
            allow_host_agent=True,
        )
    )
def test_benchmark_runner_streams_submission_and_feedback(monkeypatch, tmp_path: Path) -> None:
    import harness.benchmark.runner as runner_module

    fake_agent = FakeAgentAdapter()
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: fake_agent,
    )
    benchmark = FakeBenchmarkAdapter()
    case = BenchmarkCase(id="CASE-1", benchmark="fake")
    agent = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "demo",
            "type": "cli",
            "command": ["true"],
        }
    )

    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=case,
            agent=agent,
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
            allow_host_agent=True,
        )
    )

    assert result["success"] is True
    assert benchmark.submissions == ["flag{live}"]
    assert benchmark.closed is True
    assert fake_agent.session is not None
    assert fake_agent.session.checkpoint_calls == 3
    checkpoint = json.loads(
        (tmp_path / result["run_id"] / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["event_offset"] == 3
    assert not checkpoint["pending_actions"]
    assert fake_agent.session.closed_reason == "objective-complete"
    feedback_types = [item.type for item in fake_agent.session.feedback]
    assert "world.state.updated" in feedback_types
    assert "solver.verification" not in feedback_types
    assert "benchmark.feedback" in feedback_types
    benchmark_feedback = next(
        item for item in fake_agent.session.feedback if item.type == "benchmark.feedback"
    )
    assert benchmark_feedback.data["accepted"] is True
    assert benchmark_feedback.data["completed"] is True
    assert benchmark_feedback.data["world_revision"] >= 1
    assert result["progress"]["verified_actions"] == 0
    assert result["progress"]["pending_actions"] == 0
    assert result["progress"]["skipped_verifications"] >= 0
    assert result["progress"]["last_verification"] is None
    assert result["progress"]["accepted_submissions"] == 1
    assert result["progress"]["objective_completed"] is True
    assert result["world"]["revision"] >= 2
    context = next(tmp_path.glob("fake_CASE-1_*/world.context.txt")).read_text(encoding="utf-8")
    assert "obs-live" in context
    assert "benchmark.submission.feedback" in context


def test_benchmark_runner_tears_down_after_agent_stream_crash(monkeypatch, tmp_path: Path) -> None:
    import harness.benchmark.runner as runner_module

    fake_agent = CrashingFakeAgentAdapter()
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: fake_agent,
    )
    benchmark = FakeBenchmarkAdapter()
    agent = AgentSpec.model_validate(
        {"apiVersion": "harness/v1", "id": "demo", "type": "cli", "command": ["true"]}
    )
    with pytest.raises(RuntimeError, match="injected agent session crash"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=benchmark,
                case=BenchmarkCase(id="CASE-CRASH", benchmark="fake"),
                agent=agent,
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
                allow_host_agent=True,
            )
        )
    assert benchmark.closed is True
    assert fake_agent.session.closed_reason == "benchmark_run_error"
class InconsistentEvaluationBenchmark(FakeBenchmarkAdapter):
    async def evaluate(self, _session):
        return EvaluationResult(success=False, score=10, message="final evaluator rejected")


class MaliciousClaimSession(FakeSession):
    async def events(self):
        yield AgentEvent(
            type="action.intent",
            data={"expected_observations": ["objective completed"]},
        )
        yield AgentEvent(
            type="tool.call",
            data={"tool": "submit", "tool_call_id": "claim-action-1"},
        )
        yield AgentEvent(
            type="tool.result",
            data={
                "tool": "submit",
                "tool_call_id": "claim-action-1",
                "is_error": False,
                "observation": "the objective is complete",
            },
        )
        yield AgentEvent(
            type="progress.updated",
            data={"objective_completed": True, "confirmed_fact": "objective complete"},
        )


class MaliciousClaimAgentAdapter(FakeAgentAdapter):
    async def start_session(self, context, **_kwargs):
        self.session = MaliciousClaimSession(context.run_dir)
        return self.session


def test_tool_success_and_agent_claim_do_not_override_failed_final_evaluator(
    monkeypatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    fake_agent = MaliciousClaimAgentAdapter()
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: fake_agent,
    )
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=InconsistentEvaluationBenchmark(),
            case=BenchmarkCase(id="CASE-MALICIOUS", benchmark="fake"),
            agent=AgentSpec.model_validate(
                {"apiVersion": "harness/v1", "id": "agent", "type": "cli", "command": ["true"]}
            ),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
            allow_host_agent=True,
        )
    )

    assert result["success"] is False
    assert result["progress"]["objective_completed"] is False
    assert result["progress"]["completion_claims"] == ["goal:CASE-MALICIOUS:objective"]
    assert result["progress"]["objective_verdict"]["status"] == "contradicted"
    assert result["progress"]["last_verification"]["status"] == "pending"
    assert fake_agent.session is not None
    assert any(
        item.type == "solver.verification" and item.data["status"] == "pending"
        for item in fake_agent.session.feedback
    )
    audit = audit_run(tmp_path / result["run_id"])
    assert audit.status == "incomplete"
    assert audit.counts["untraceable_verdicts"] == 0
    assert not any(issue.severity == "error" for issue in audit.issues)


class CrashAfterToolDispatchSession(FakeSession):
    async def events(self):
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="tool.call",
            data={"tool": "write", "tool_call_id": "interrupted-call"},
        )
        raise RuntimeError("simulated process crash after dispatch")


class CrashAfterToolDispatchAgentAdapter(FakeAgentAdapter):
    async def start_session(self, context, **_kwargs):
        self.session = CrashAfterToolDispatchSession(context.run_dir)
        return self.session


def test_checkpoint_keeps_dispatched_action_pending_after_stream_crash(
    monkeypatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    fake_agent = CrashAfterToolDispatchAgentAdapter()
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: fake_agent,
    )
    runner = BenchmarkRunner(runs_root=tmp_path)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        asyncio.run(
            runner.run_case(
                adapter=FakeBenchmarkAdapter(),
                case=BenchmarkCase(id="CASE-CRASH", benchmark="fake"),
                agent=AgentSpec.model_validate(
                    {"apiVersion": "harness/v1", "id": "agent", "type": "cli", "command": ["true"]}
                ),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
                allow_host_agent=True,
            )
        )

    run_dir = next(tmp_path.glob("fake_CASE-CRASH_*"))
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["event_offset"] == 1
    assert [action["tool_call_id"] for action in checkpoint["pending_actions"]] == [
        "interrupted-call"
    ]
    with pytest.raises(RuntimeError, match="unresolved actions"):
        from harness.runtime.checkpoint import load_recovery_state

        load_recovery_state(run_dir)
