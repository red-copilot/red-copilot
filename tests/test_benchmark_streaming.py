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
from harness.session import RPC_AGENT_CAPABILITIES, AgentCheckpoint, AgentEvent


def _pi_agent(*, agent_id: str = "demo", network: str = "host") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": agent_id,
            "image": "pi:test",
            "network": network,
            "pi": {"model": "fake-model"},
        }
    )


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
    agent = _pi_agent(agent_id="container", network="environment")

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
                agent=_pi_agent(),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
            )
        )

    assert adapter.closed is True
    assert adapter.teardown_calls == 1
def test_benchmark_provision_error_leaves_no_run_directory(tmp_path: Path) -> None:
    class FailingProvision(FakeBenchmarkAdapter):
        async def provision(self, _case):
            raise RuntimeError("sensitive opaque provider payload provision-secret-canary")

    with pytest.raises(RuntimeError, match="provision-secret-canary"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=FailingProvision(),
                case=BenchmarkCase(id="CASE-PROVISION-ERROR", benchmark="fake"),
                agent=_pi_agent(),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
            )
        )

    # A refused start must leave nothing behind: the 2026-10-16 TSec sweep
    # accumulated one empty run directory per selected challenge per attempt.
    assert list(tmp_path.glob("fake_CASE-PROVISION-ERROR_*")) == []
    assert "provision-secret-canary" not in "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


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
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
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
                agent=_pi_agent(),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
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
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
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
    capabilities = RPC_AGENT_CAPABILITIES

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
    agent = _pi_agent()
    return asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=adapter if adapter is not None else FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-BUDGET", benchmark="fake"),
            agent=agent,
            budgets=budgets,
            seed=1,
            submission_extractor=lambda _result: [],
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
    agent = _pi_agent()

    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=case,
            agent=agent,
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
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
    assert result["progress"]["submissions_attempted"] == 1
    assert result["progress"]["objective_completed"] is True
    # A scored run has to be able to say "we never tried" apart from "we were
    # wrong"; the sweep summary and the run metrics both read this pair.
    assert result["metrics"]["submissions_attempted"] == 1
    assert isinstance(result["metrics"]["first_submission_ms"], int)
    # The run-level metric block answers "how much did the loop talk to the
    # Agent, how long did tools block it, and how big was the final prompt"
    # without re-parsing the trace.
    assert result["metrics"]["steer_counts"] == {
        "world": 0,
        "verification": 0,
        "repetition": 0,
        "budget": 0,
        "finalize": 0,
    }
    assert result["metrics"]["tool_calls"] == 0
    assert result["metrics"]["tool_time_ms"] == 0
    assert result["metrics"]["context_tokens_final"] == 0
    assert result["metrics"]["slow_tool_calls"] == []
    assert 0.0 <= result["metrics"]["submitted_at_fraction"] <= 1.0
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
    agent = _pi_agent()
    with pytest.raises(RuntimeError, match="injected agent session crash"):
        asyncio.run(
            BenchmarkRunner(runs_root=tmp_path).run_case(
                adapter=benchmark,
                case=BenchmarkCase(id="CASE-CRASH", benchmark="fake"),
                agent=agent,
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
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
            agent=_pi_agent(agent_id="agent"),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    assert result["success"] is False
    assert result["progress"]["objective_completed"] is False
    assert result["progress"]["completion_claims"] == ["goal:CASE-MALICIOUS:objective"]
    assert result["progress"]["objective_verdict"]["status"] == "contradicted"
    # A benchmark run has no independent observation to compare an action
    # against, so the honest verdict is "unavailable": recorded, never steered,
    # never mistaken for progress or for a stall (VER-05).
    assert result["progress"]["last_verification"]["status"] == "unavailable"
    assert result["progress"]["last_verification"]["replan_required"] is False
    assert result["progress"]["unavailable_actions"] >= 1
    assert fake_agent.session is not None
    # Steering on a non-verdict is noise; it stays recorded in the progress
    # ledger and the trace but is no longer injected into the session.
    assert not any(
        item.type == "solver.verification" for item in fake_agent.session.feedback
    )
    verdicts = [
        json.loads(line)
        for line in (tmp_path / result["run_id"] / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(
        item.get("type") == "action.verified"
        and item.get("data", {}).get("status") == "unavailable"
        for item in verdicts
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
                agent=_pi_agent(agent_id="agent"),
                budgets=BudgetSpec(wall_time=30),
                seed=1,
                submission_extractor=lambda _result: [],
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


def test_gc_runs_reports_and_removes_only_artifact_free_run_directories(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from harness.cli import app
    from harness.runtime.results import find_empty_run_dirs

    empty_run = tmp_path / "tsec_a-01_deadbeef"
    (empty_run / "agent-workspace" / "notes").mkdir(parents=True)
    traced_run = tmp_path / "tsec_a-02_cafebabe"
    traced_run.mkdir()
    (traced_run / "trace.jsonl").write_text("", encoding="utf-8")
    finished_run = tmp_path / "tsec_a-03_0123abcd"
    finished_run.mkdir()
    (finished_run / "result.json").write_text("{}", encoding="utf-8")

    assert find_empty_run_dirs(tmp_path) == [empty_run]

    runner = CliRunner()
    dry = runner.invoke(app, ["gc-runs", "--root", str(tmp_path)])
    assert dry.exit_code == 0
    assert str(empty_run) in dry.stdout
    assert "dry run" in dry.stderr
    assert empty_run.exists()

    applied = runner.invoke(app, ["gc-runs", "--root", str(tmp_path), "--apply"])
    assert applied.exit_code == 0
    assert not empty_run.exists()
    assert traced_run.exists() and finished_run.exists()


def test_report_prints_the_loop_health_block(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from harness.cli import app

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": "harness/result/v2",
                "run_id": "run-1",
                "status": "finished",
                "success": False,
                "score": 0.0,
                "metrics": {
                    "duration_ms": 1800000,
                    "submissions_attempted": 0,
                    "first_submission_ms": None,
                    "submitted_at_fraction": None,
                    "steer_counts": {
                        "world": 99,
                        "verification": 39,
                        "repetition": 3,
                        "budget": 3,
                        "finalize": 1,
                    },
                    "tool_time_ms": 813000,
                    "tool_calls": 96,
                    "tool_failures": 12,
                    "context_tokens_final": 44311,
                    "slow_tool_calls": [
                        {"tool": "bash", "duration_ms": 900000, "tool_call_id": "c1"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    output = CliRunner().invoke(app, ["report", str(run_dir)]).stdout

    assert "run metrics:" in output
    assert "candidates attempted: 0" in output
    assert "submitted: never" in output
    assert "world=99" in output
    assert "verification=39" in output
    assert "813000 ms over 96 calls (12 failed)" in output
    assert "44311 tokens" in output
    assert "bash 900000 ms" in output


def test_report_tolerates_a_result_without_metrics(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from harness.cli import app

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(
        json.dumps({"schema_version": "harness/result/v2", "run_id": "old"}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["report", str(run_dir)])

    assert result.exit_code == 0
    assert "run metrics:" not in result.stdout


def test_benchmark_runs_keep_the_four_closure_switches_on(monkeypatch, tmp_path: Path) -> None:
    """Pin the defaults the failure shape of 2026-10-16 depends on.

    Fifteen losing runs submitted nothing and spent turns on feedback they could
    not act on; the benchmark path has to keep asking for a submission at the
    soft limit and stop echoing zero-information steers.
    """
    import harness.benchmark.runner as runner_module

    captured: list[dict] = []
    real_bootstrap = runner_module.bootstrap_solver

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real_bootstrap(*args, **kwargs)

    monkeypatch.setattr(runner_module, "bootstrap_solver", spy)
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: FakeAgentAdapter(),
    )

    asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-SWITCHES", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    assert captured, "the benchmark run must compose a solver runtime"
    for kwargs in captured:
        assert kwargs["finalize_requests_submission"] is True
        assert kwargs["submission_due_fraction"] == runner_module.BENCHMARK_SUBMISSION_DUE_FRACTION
        assert kwargs["quiet_world_steers"] is True
        assert kwargs["verdict_steers_only"] is True


class RejectingBenchmarkAdapter(FakeBenchmarkAdapter):
    """The platform refuses the candidate: the one real mid-run signal there is."""

    async def submit(self, _session, submission):
        self.submissions.append(submission.value)
        return SubmissionResult(
            accepted=False,
            score_delta=0,
            completed=False,
            metadata={"correct_flag_count": 0, "total_flag_count": 1},
        )


class ObservationSession(FakeSession):
    """Runs a tool call, submits a candidate, then keeps working."""

    async def events(self):
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="action.intent",
            data={
                "description": "probe the admin endpoint",
                "expected_observations": ["admin endpoint reachable"],
            },
        )
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="tool.call",
            data={"tool": "bash", "tool_call_id": "call-obs", "args_family": "bash:curl"},
        )
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="tool.result",
            data={
                "tool": "bash",
                "tool_call_id": "call-obs",
                "duration_ms": 10,
                "is_error": False,
                "exit_code": 0,
            },
        )
        (self.run_dir / "submission.inbox.jsonl").write_text(
            json.dumps({"type": "flag", "value": "flag{guess}"}) + "\n",
            encoding="utf-8",
        )
        self.checkpoint_calls += 1
        yield AgentEvent(type="progress.updated", data={"step": 2, "made_progress": True})
        # Keep working after the rejected candidate: the next action is the one
        # the platform verdict can speak for.
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="tool.call",
            data={"tool": "bash", "tool_call_id": "call-obs-2", "args_family": "bash:curl"},
        )
        self.checkpoint_calls += 1
        yield AgentEvent(
            type="tool.result",
            data={
                "tool": "bash",
                "tool_call_id": "call-obs-2",
                "duration_ms": 20,
                "is_error": False,
                "exit_code": 0,
            },
        )


class ObservationAgentAdapter(FakeAgentAdapter):
    async def start_session(self, context, **_kwargs):
        self.session = ObservationSession(context.run_dir)
        return self.session


def test_platform_verdict_is_the_comparison_baseline_for_the_next_action(
    monkeypatch, tmp_path: Path
) -> None:
    """VER-05: the Harness compares against what it saw, not what the Agent said."""
    import harness.benchmark.runner as runner_module

    fake_agent = ObservationAgentAdapter()
    monkeypatch.setattr(
        runner_module,
        "build_agent_adapter",
        lambda *_args, **_kwargs: fake_agent,
    )

    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=RejectingBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-OBS", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    verification = result["progress"]["last_verification"]
    assert verification is not None
    assert verification["status"] == "pending"
    assert verification["replan_required"] is False
    assert "observation_recorded" in verification["replan_reasons"]
    observation = verification["observation"]
    assert observation["producer"] == "benchmark_evaluator"
    assert observation["kind"] == "benchmark.submission.verdict"
    assert "rejected" in observation["summary"]
    assert verification["actual_observation"] == observation["summary"]
    # A non-verdict is recorded, never steered.
    assert fake_agent.session is not None
    assert not any(
        item.type == "solver.verification" for item in fake_agent.session.feedback
    )
