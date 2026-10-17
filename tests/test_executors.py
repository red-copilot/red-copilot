from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.agent import AgentResult, task_environment
from harness.benchmark.base import (
    BenchmarkCase,
    BenchmarkSession,
    EvaluationResult,
    SubmissionResult,
)
from harness.benchmark.runner import BenchmarkRunner
from harness.budget import UsageMetrics
from harness.models import MAX_EXECUTOR_SESSIONS, AgentSpec, BudgetSpec, ObjectiveSpec
from harness.pi_container import ContainerPiAdapter
from harness.progress import ProgressLedger
from harness.runtime import executors as executors_module
from harness.runtime.executors import (
    EXECUTOR_DIRNAME,
    SHARED_DIRNAME,
    SHARED_FINDINGS_NAME,
    FindingsArchive,
    fold_session_progress,
    plan_executor_sessions,
    prepare_shared_findings,
    split_budgets,
)
from harness.session import AgentCheckpoint, AgentEvent
from harness.trace import TraceRecorder


def _pi_agent(executors: list[dict] | None = None) -> AgentSpec:
    payload: dict = {
        "apiVersion": "harness/v1",
        "id": "demo",
        "type": "pi",
        "image": "probe",
        "network": "host",
        "pi": {"model": "test-model", "provider": "openai"},
    }
    if executors:
        payload["executors"] = executors
    return AgentSpec.model_validate(payload)


class FakeBenchmarkAdapter:
    """One provisioned case; every submit is recorded for dedupe assertions."""

    def __init__(self) -> None:
        self.provision_calls = 0
        self.submissions: list[str] = []
        self.teardown_calls = 0

    async def provision(self, case: BenchmarkCase) -> BenchmarkSession:
        self.provision_calls += 1
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
            metadata={},
        )

    async def evaluate(self, _session):
        return EvaluationResult(success=True, score=10)

    async def teardown(self, _session):
        self.teardown_calls += 1


class ScriptedSession:
    """Agent session double: emit scripted submissions, then optionally block."""

    def __init__(self, workspace: Path, *, submissions: list[str], wait: bool) -> None:
        self.workspace = workspace
        self.submissions = submissions
        self.wait = wait
        self.feedback: list = []
        self.closed_reason: str | None = None
        self.cancelled = False
        self.checkpoint_calls = 0

    async def events(self):
        try:
            for value in self.submissions:
                self.workspace.mkdir(parents=True, exist_ok=True)
                inbox = self.workspace / "submission.inbox.jsonl"
                with inbox.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "flag", "value": value}) + "\n")
                self.checkpoint_calls += 1
                yield AgentEvent(type="progress.updated", data={"step": 1})
            if self.wait:
                self.checkpoint_calls += 1
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

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
            metrics=UsageMetrics(model_calls=1),
        )


class ScriptedAgentAdapter:
    """Per-session behaviour keyed by the session root directory name."""

    def __init__(
        self,
        behaviours: dict[str, tuple[list[str], bool]],
        *,
        default: tuple[list[str], bool] | None = None,
    ) -> None:
        self.behaviours = behaviours
        self.default = default
        self.sessions: dict[str, ScriptedSession] = {}
        self.shared_findings_dirs: dict[str, Path | None] = {}

    async def start_session(self, context):
        root = Path(context.run_dir).parent.name
        behaviour = self.behaviours.get(root, self.default)
        if behaviour is None:
            raise AssertionError(f"unexpected competing session root: {root}")
        submissions, wait = behaviour
        session = ScriptedSession(Path(context.run_dir), submissions=submissions, wait=wait)
        self.sessions[root] = session
        self.shared_findings_dirs[root] = context.shared_findings_dir
        return session


def _run_competing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    behaviours: dict[str, tuple[list[str], bool]],
    executors: list[dict],
    adapter: FakeBenchmarkAdapter | None = None,
    default: tuple[list[str], bool] | None = None,
) -> tuple[dict, FakeBenchmarkAdapter, ScriptedAgentAdapter]:
    import harness.benchmark.runner as runner_module

    agent_adapter = ScriptedAgentAdapter(behaviours, default=default)
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )
    benchmark = adapter if adapter is not None else FakeBenchmarkAdapter()
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=BenchmarkCase(id="CASE-COMPETE", benchmark="fake"),
            agent=_pi_agent(executors),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )
    return result, benchmark, agent_adapter


def test_split_budgets_shares_usage_and_keeps_the_wall_clock() -> None:
    budgets = BudgetSpec(
        wall_time=3600,
        max_tokens=1001,
        max_model_calls=10,
        max_tool_calls=7,
        max_cost_usd=1.0,
    )

    share = split_budgets(budgets, 2)

    assert share.wall_time == 3600
    assert share.max_tokens == 500
    assert share.max_model_calls == 5
    assert share.max_tool_calls == 3
    assert share.max_cost_usd == pytest.approx(0.5)
    assert split_budgets(budgets, 1) == budgets
    with pytest.raises(ValueError, match="at least 1"):
        split_budgets(budgets, 0)


def test_unset_budget_dimensions_stay_unset_in_every_share() -> None:
    share = split_budgets(BudgetSpec(wall_time=60), 3)

    assert share.max_tokens is None
    assert share.max_model_calls is None
    assert share.max_cost_usd is None
    assert share.wall_time == 60


def test_plan_without_executors_keeps_the_single_session_layout(tmp_path: Path) -> None:
    plan = plan_executor_sessions(
        agent=_pi_agent(), budgets=BudgetSpec(wall_time=30), run_dir=tmp_path
    )

    assert plan.competing is False
    assert plan.shared_dir is None
    assert plan.findings_path is None
    (session,) = plan.sessions
    assert session.root == tmp_path
    assert session.workspace == tmp_path / "agent-workspace"


def test_plan_isolates_each_competing_session_under_its_own_root(tmp_path: Path) -> None:
    plan = plan_executor_sessions(
        agent=_pi_agent([{"id": "primary"}, {"id": "challenger"}]),
        budgets=BudgetSpec(wall_time=30, max_model_calls=20),
        run_dir=tmp_path,
    )

    assert plan.competing is True
    assert plan.shared_dir == tmp_path / SHARED_DIRNAME
    roots = [session.root for session in plan.sessions]
    assert roots == [
        tmp_path / EXECUTOR_DIRNAME / "01-primary",
        tmp_path / EXECUTOR_DIRNAME / "02-challenger",
    ]
    assert len(set(roots)) == 2
    assert all(
        session.workspace == session.root / "agent-workspace" for session in plan.sessions
    )
    assert [session.budgets.max_model_calls for session in plan.sessions] == [10, 10]
    assert [session.budgets.wall_time for session in plan.sessions] == [30, 30]
    assert len({session.session_id for session in plan.sessions}) == 2


def test_configuration_above_the_session_ceiling_fails_closed() -> None:
    with pytest.raises(ValidationError, match=f"at most {MAX_EXECUTOR_SESSIONS}"):
        _pi_agent([{"id": f"executor-{index}"} for index in range(MAX_EXECUTOR_SESSIONS + 1)])


def test_scheduler_rejects_a_session_count_above_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(executors_module, "MAX_EXECUTOR_SESSIONS", 1)

    with pytest.raises(ValueError, match="at most 1"):
        plan_executor_sessions(
            agent=_pi_agent([{"id": "primary"}, {"id": "challenger"}]),
            budgets=BudgetSpec(wall_time=30),
            run_dir=tmp_path,
        )


def test_shared_findings_file_is_created_once_and_archived_on_change(tmp_path: Path) -> None:
    plan = plan_executor_sessions(
        agent=_pi_agent([{"id": "primary"}, {"id": "challenger"}]),
        budgets=BudgetSpec(wall_time=30),
        run_dir=tmp_path,
    )
    findings = prepare_shared_findings(plan)
    assert findings is not None and findings.parent == tmp_path / SHARED_DIRNAME
    prepare_shared_findings(plan)

    assert [path.name for path in (tmp_path / SHARED_DIRNAME).iterdir()] == [
        SHARED_FINDINGS_NAME
    ]
    archive = FindingsArchive(run_dir=tmp_path)
    assert archive.snapshot() is not None
    assert archive.snapshot() is None
    findings.write_text("# Shared findings\n\n- target accepts GET /admin\n", encoding="utf-8")
    assert archive.snapshot() is not None
    archived = sorted((tmp_path / SHARED_DIRNAME / "archive").glob("*.md"))
    assert len(archived) == 2
    assert "GET /admin" in archived[-1].read_text(encoding="utf-8")


def test_shared_directory_holds_no_authoritative_artifacts(tmp_path: Path) -> None:
    plan = plan_executor_sessions(
        agent=_pi_agent([{"id": "primary"}, {"id": "challenger"}]),
        budgets=BudgetSpec(wall_time=30),
        run_dir=tmp_path,
    )
    prepare_shared_findings(plan)
    FindingsArchive(run_dir=tmp_path).snapshot()
    (tmp_path / "world.db").write_bytes(b"")
    (tmp_path / "progress.json").write_text("{}", encoding="utf-8")
    (tmp_path / "result.json").write_text("{}", encoding="utf-8")

    shared = tmp_path / SHARED_DIRNAME
    names = {
        path.relative_to(shared).as_posix() for path in shared.rglob("*") if path.is_file()
    }
    assert SHARED_FINDINGS_NAME in names
    for name in names:
        assert Path(name).name not in {
            "world.db",
            "progress.json",
            "result.json",
            "checkpoint.json",
        }


def test_fold_session_progress_sums_session_counters_and_budget() -> None:
    run_progress = ProgressLedger(
        active_goal="goal:1", budget_limits={"max_tool_calls": 10.0}
    )
    first = ProgressLedger()
    first.tool_calls = 2
    first.model_calls = 1
    first.budget_used = {"tokens": 100.0}
    first.failed_skill_counts = {"web-crawl": 1}
    first.record_submission(accepted=True, completed=False)
    second = ProgressLedger()
    second.tool_calls = 3
    second.model_calls = 4
    second.budget_used = {"tokens": 50.0}
    second.record_submission(accepted=False, completed=False)

    fold_session_progress(run_progress, (first, second))

    assert run_progress.tool_calls == 5
    assert run_progress.model_calls == 5
    assert run_progress.budget_used == {"tokens": 150.0}
    assert run_progress.failed_skill_counts == {"web-crawl": 1}
    assert run_progress.accepted_submissions == 1
    assert run_progress.rejected_submissions == 1


def test_competing_sessions_share_one_provision_and_dedupe_submissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, benchmark, agent_adapter = _run_competing(
        monkeypatch,
        tmp_path,
        behaviours={
            "01-primary": (["flag{shared}"], False),
            "02-challenger": (["flag{shared}", "flag{extra}"], True),
        },
        executors=[{"id": "primary"}, {"id": "challenger"}],
    )

    assert benchmark.provision_calls == 1
    assert benchmark.teardown_calls == 1
    assert sorted(benchmark.submissions) == ["flag{extra}", "flag{shared}"]
    assert result["success"] is True
    assert result["executor_sessions"][0]["objective_completed"] is True
    assert agent_adapter.sessions["01-primary"].closed_reason == "objective-complete"


def test_first_success_cancels_the_remaining_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _benchmark, agent_adapter = _run_competing(
        monkeypatch,
        tmp_path,
        behaviours={
            "01-primary": (["flag{winner}"], False),
            "02-challenger": ([], True),
        },
        executors=[{"id": "primary"}, {"id": "challenger"}],
    )

    assert agent_adapter.sessions["02-challenger"].cancelled is True
    statuses = {item["executor_id"]: item["status"] for item in result["executor_sessions"]}
    assert statuses == {"primary": "completed", "challenger": "cancelled"}
    assert result["termination_reason"] == "objective-complete"


def test_competing_sessions_get_isolated_workspaces_and_the_shared_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _benchmark, agent_adapter = _run_competing(
        monkeypatch,
        tmp_path,
        behaviours={
            "01-primary": (["flag{winner}"], False),
            "02-challenger": ([], True),
        },
        executors=[{"id": "primary"}, {"id": "challenger"}],
    )
    run_dir = tmp_path / result["run_id"]

    assert agent_adapter.shared_findings_dirs == {
        "01-primary": run_dir / SHARED_DIRNAME,
        "02-challenger": run_dir / SHARED_DIRNAME,
    }
    (run_dir / SHARED_DIRNAME / SHARED_FINDINGS_NAME).is_file()
    for name in ("01-primary", "02-challenger"):
        session_root = run_dir / EXECUTOR_DIRNAME / name
        assert (session_root / "agent-workspace" / "notes").is_dir()
        assert (session_root / "checkpoint.json").is_file()
        assert (session_root / "progress.json").is_file()
    assert not (run_dir / "agent-workspace").exists()
    assert not (run_dir / "checkpoint.json").exists()


def test_one_failing_session_does_not_end_the_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    class FlakyAdapter(ScriptedAgentAdapter):
        async def start_session(self, context):
            root = Path(context.run_dir).parent.name
            if root == "01-primary":
                raise RuntimeError("injected session failure")
            return await super().start_session(context)

    agent_adapter = FlakyAdapter({"02-challenger": (["flag{late}"], False)})
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )
    benchmark = FakeBenchmarkAdapter()
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=BenchmarkCase(id="CASE-COMPETE", benchmark="fake"),
            agent=_pi_agent([{"id": "primary"}, {"id": "challenger"}]),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    assert benchmark.submissions == ["flag{late}"]
    assert result["success"] is True
    statuses = {item["executor_id"]: item["status"] for item in result["executor_sessions"]}
    assert statuses == {"primary": "error", "challenger": "completed"}
    trace = (tmp_path / result["run_id"] / "trace.jsonl").read_text(encoding="utf-8")
    assert "executor.session.error" in trace
    assert "injected session failure" not in trace


def test_single_session_runs_keep_the_historical_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    agent_adapter = ScriptedAgentAdapter({}, default=(["flag{solo}"], False))
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )
    benchmark = FakeBenchmarkAdapter()
    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=benchmark,
            case=BenchmarkCase(id="CASE-SOLO", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )
    run_dir = tmp_path / result["run_id"]

    assert benchmark.submissions == ["flag{solo}"]
    assert "executor_sessions" not in result
    assert (run_dir / "agent-workspace").is_dir()
    assert (run_dir / "checkpoint.json").is_file()
    assert not (run_dir / SHARED_DIRNAME).exists()
    assert agent_adapter.shared_findings_dirs == {run_dir.name: None}


def test_container_agent_gets_the_shared_findings_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    adapter = ContainerPiAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-agent",
                "type": "pi",
                "image": "harness-pi-kali:test",
                "pi": {"provider": "fake", "model": "m", "mode": "json"},
            }
        ),
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task"),
    )
    task_dir = Path("benchmarks/examples/hello").resolve()
    run_dir = tmp_path / "run-pi"
    run_dir.mkdir()
    shared_dir = tmp_path / SHARED_DIRNAME
    shared_dir.mkdir()
    (shared_dir / SHARED_FINDINGS_NAME).write_text("# Shared findings\n", encoding="utf-8")
    command = adapter._container_command(
        _task_spec(),
        task_dir=task_dir,
        run_dir=run_dir,
        container_name="pi-agent",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
        shared_findings_dir=shared_dir,
    )

    assert f"{shared_dir.resolve()}:/run/harness-shared:rw" in command
    assert "HARNESS_SHARED_FINDINGS=/run/harness-shared/shared-findings.md" in command


def _task_spec():
    from harness.models import TaskSpec

    return TaskSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "task",
            "name": "Task",
            "objective": {"description": "test"},
        }
    )


def test_task_environment_omits_the_shared_findings_channel_without_a_share() -> None:
    environment = task_environment(
        _task_spec(),
        task_dir="/task",
        run_dir="/run/harness",
        environment_project=None,
        seed=1,
        network_profile="benchmark-only",
        shared_findings_file="/run/harness-shared/shared-findings.md",
    )
    assert environment["HARNESS_SHARED_FINDINGS"] == "/run/harness-shared/shared-findings.md"

    plain = task_environment(
        _task_spec(),
        task_dir="/task",
        run_dir="/run/harness",
        environment_project=None,
        seed=1,
        network_profile="benchmark-only",
    )
    assert "HARNESS_SHARED_FINDINGS" not in plain


def test_competing_sessions_report_per_prompt_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executors = [
        {"id": "primary", "system_prompt_file": "prompts/zc-solver.md"},
        {"id": "challenger", "model": "other-model", "provider": "openai"},
    ]
    result, _benchmark, _adapter = _run_competing(
        monkeypatch,
        tmp_path,
        behaviours={
            "01-primary": (["flag{primary}"], False),
            "02-challenger": (["flag{challenger}"], False),
        },
        executors=executors,
    )

    sessions = {item["executor_id"]: item for item in result["executor_sessions"]}
    assert sessions["primary"]["prompt"] == "prompts/zc-solver.md"
    assert sessions["primary"]["provider"] == "openai"
    assert sessions["primary"]["model"] == "test-model"
    assert sessions["challenger"]["prompt"] is None
    assert sessions["challenger"]["model"] == "other-model"

    summary = {entry["model"]: entry for entry in result["executor_summary"]}
    assert summary["test-model"]["sessions"] == 1
    assert summary["test-model"]["prompt"] == "prompts/zc-solver.md"
    assert summary["other-model"]["sessions"] == 1
    assert summary["other-model"]["model"] == "other-model"
    assert len(result["executor_summary"]) == 2


def test_single_session_run_carries_no_executor_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, _benchmark, _adapter = _run_competing(
        monkeypatch,
        tmp_path,
        behaviours={},
        executors=[],
        default=(["flag{solo}"], False),
    )

    assert "executor_sessions" not in result
    assert "executor_summary" not in result


class SummaryAgentAdapter:
    """Session double that writes (or omits) the final report."""

    def __init__(self, summary: str | None) -> None:
        self.summary = summary
        self.session: ScriptedSession | None = None

    async def start_session(self, context):
        workspace = Path(context.run_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        if self.summary is not None:
            (workspace / "agent.summary.md").write_text(self.summary, encoding="utf-8")
        self.session = ScriptedSession(workspace, submissions=[], wait=False)
        return self.session


def _run_single_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, summary: str | None
) -> dict:
    import harness.benchmark.runner as runner_module

    agent_adapter = SummaryAgentAdapter(summary)
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )
    return asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-SUMMARY", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )


def test_agent_report_records_missing_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_single_summary(
        monkeypatch,
        tmp_path,
        "## Confirmed facts\nflag{ok}\n\n## Unfinished intents\nnone\n",
    )

    report = result["agent_report"]
    assert report["present"] is True
    assert report["sections_missing"] == ["unconfirmed", "ruled out"]
    trace = (tmp_path / result["run_id"] / "trace.jsonl").read_text(encoding="utf-8")
    assert "solver.summary_incomplete" in trace
    assert '"reason":"sections_missing"' in trace


def test_agent_report_records_a_missing_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_single_summary(monkeypatch, tmp_path, None)

    report = result["agent_report"]
    assert report["present"] is False
    assert report["sections_missing"] == [
        "confirmed",
        "unconfirmed",
        "ruled out",
        "unfinished",
    ]
    trace = (tmp_path / result["run_id"] / "trace.jsonl").read_text(encoding="utf-8")
    assert '"reason":"summary_missing"' in trace


def test_agent_report_does_not_read_an_unconfirmed_heading_as_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _run_single_summary(
        monkeypatch,
        tmp_path,
        "## Unconfirmed hypotheses\nmay reach the admin panel\n",
    )

    report = result["agent_report"]
    assert report["present"] is True
    # "confirmed" is a substring of "unconfirmed"; the missing-check must still
    # report the confirmed-facts section as absent.
    assert report["sections_missing"] == ["confirmed", "ruled out", "unfinished"]


def test_seed_notes_carry_the_previous_summary_into_every_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module

    previous = tmp_path / "previous-run"
    previous.mkdir()
    (previous / "agent.summary.md").write_text(
        "## Confirmed facts\nflag{prior}\n", encoding="utf-8"
    )
    agent_adapter = ScriptedAgentAdapter(
        behaviours={
            "01-primary": ([], False),
            "02-challenger": ([], False),
        }
    )
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )

    asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-SEED", benchmark="fake"),
            agent=_pi_agent(
                [{"id": "primary"}, {"id": "challenger", "model": "other-model"}]
            ),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
            seed_notes_from=previous,
        )
    )

    seeded = sorted(
        (tmp_path).glob(f"fake_CASE-SEED_*/{EXECUTOR_DIRNAME}/*/agent-workspace/notes/*.md")
    )
    assert len(seeded) == 2
    for path in seeded:
        assert path.name == "prior-summary.md"
        assert "flag{prior}" in path.read_text(encoding="utf-8")


def test_seed_notes_requires_a_previous_summary(tmp_path: Path) -> None:
    from harness.agent_workspace import seed_notes_from_summary

    previous = tmp_path / "previous-run"
    previous.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="seed notes require an existing summary"):
        seed_notes_from_summary(workspace=workspace, previous_run_dir=previous)


class RestartableAgentAdapter:
    """Session double that can be restarted, like the container Pi adapter."""

    def __init__(self, *, events_per_session: list[int]) -> None:
        self.events_per_session = events_per_session
        self.sessions: list[RestartableSession] = []

    async def start_session(self, context):
        workspace = Path(context.run_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        index = len(self.sessions)
        events = self.events_per_session[
            min(index, len(self.events_per_session) - 1)
        ]
        session = RestartableSession(
            workspace=workspace,
            identity=f"session-{index + 1}",
            events=events,
        )
        self.sessions.append(session)
        return session


class RestartableSession:
    def __init__(self, *, workspace: Path, identity: str, events: int) -> None:
        self.workspace = workspace
        self.identity = identity
        self.events_to_emit = events
        self.feedback: list = []
        self.closed_reason: str | None = None

    async def events(self):
        for index in range(self.events_to_emit):
            yield AgentEvent(
                type="tool.call",
                event_id=f"{self.identity}-{index}",
                data={
                    "tool": "bash",
                    "tool_call_id": f"{self.identity}-call-{index}",
                    "args_family": f"bash:cmd-{self.identity}-{index}",
                },
            )

    async def checkpoint(self) -> AgentCheckpoint:
        return AgentCheckpoint(
            id=f"checkpoint-{self.identity}",
            event_offset=self.events_to_emit,
            feedback_offset=0,
            session_id=self.identity,
        )

    async def observe(self, observation) -> None:
        self.feedback.append(observation)

    async def close(self, reason: str) -> None:
        self.closed_reason = reason

    async def result(self) -> AgentResult:
        return AgentResult(
            returncode=0,
            timed_out=False,
            budget_exceeded=None,
            stdout="",
            stderr="",
            metrics=UsageMetrics(model_calls=1, total_tokens=7),
        )


def test_long_session_is_restarted_with_a_carry_forward_brief(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module
    import harness.progress as progress_module

    monkeypatch.setattr(progress_module, "CONTEXT_RESTART_TOOL_CALLS", 3)
    agent_adapter = RestartableAgentAdapter(events_per_session=[3, 1])
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )

    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-RESTART", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    # Two sessions ran: the first was retired at the threshold.
    assert len(agent_adapter.sessions) == 2
    first, second = agent_adapter.sessions
    assert first.closed_reason == "context-restart:tool_calls"
    briefs = [item for item in second.feedback if item.type == "solver.context_restart"]
    assert len(briefs) == 1
    assert "# Carry-forward brief" in briefs[0].data["message"]
    assert briefs[0].data["reason"] == "tool_calls"

    root = tmp_path / result["run_id"]
    brief_file = root / "carry-forward.md"
    assert brief_file.is_file()
    assert "# Carry-forward brief" in brief_file.read_text(encoding="utf-8")
    trace = (root / "trace.jsonl").read_text(encoding="utf-8")
    assert "solver.context_restart" in trace
    assert '"from_session":"session-1"' in trace
    assert '"to_session":"session-2"' in trace
    # The retired session's usage stays in the run accounting.
    assert result["metrics"]["total_tokens"] == 14


def test_context_restart_gives_up_after_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.benchmark.runner as runner_module
    import harness.progress as progress_module

    monkeypatch.setattr(progress_module, "CONTEXT_RESTART_TOOL_CALLS", 2)
    monkeypatch.setattr(progress_module, "MAX_CONTEXT_RESTARTS", 2)
    agent_adapter = RestartableAgentAdapter(events_per_session=[2, 2, 2])
    monkeypatch.setattr(
        runner_module, "build_agent_adapter", lambda *_args, **_kwargs: agent_adapter
    )

    result = asyncio.run(
        BenchmarkRunner(runs_root=tmp_path).run_case(
            adapter=FakeBenchmarkAdapter(),
            case=BenchmarkCase(id="CASE-RESTART-CAP", benchmark="fake"),
            agent=_pi_agent(),
            budgets=BudgetSpec(wall_time=30),
            seed=1,
            submission_extractor=lambda _result: [],
        )
    )

    assert len(agent_adapter.sessions) == 3
    progress = json.loads(
        (tmp_path / result["run_id"] / "agent-workspace" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["context_restarts"] == 2
