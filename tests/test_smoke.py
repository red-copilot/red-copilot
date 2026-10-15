import asyncio
import hashlib
import json
from pathlib import Path

from harness.agent import AgentResult
from harness.agent_workspace import create_agent_workspace
from harness.budget import UsageMetrics
from harness.models import load_agent, load_task
from harness.orchestrator import Orchestrator
from harness.runtime.bootstrap import bootstrap_solver
from harness.runtime.checkpoint import save_session_checkpoint
from harness.session import AgentCheckpoint, AgentEvent
from harness.trace import TraceRecorder
from harness.world import Goal


def test_smoke_run(tmp_path: Path) -> None:
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/demo.yaml")
    result = Orchestrator(tmp_path / "runs").run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=42,
        run_id="run_worker_owned",
    )
    assert result["run_id"] == "run_worker_owned"
    assert result["status"] == "finished"
    assert result["success"] is True
    run_dir = tmp_path / "runs" / result["run_id"]
    assert (run_dir / "world.db").is_file()
    assert result["score"] == 100
    assert result["seed"] == 42
    assert result["metrics"]["total_tokens"] == 12
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["tool_calls"] == 1
    assert result["metrics"]["cost_usd"] == 0.01


def test_unavailable_task_verifier_is_recorded_and_run_fails_closed(monkeypatch, tmp_path: Path) -> None:
    from harness.audit import audit_run
    from harness.verifier import VerifierError

    def unavailable(*_args, **_kwargs):
        raise VerifierError("simulated verifier timeout with private details")

    monkeypatch.setattr("harness.orchestrator.run_verifier", unavailable)
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/demo.yaml")
    result = Orchestrator(tmp_path / "runs").run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=1,
    )

    run_dir = tmp_path / "runs" / result["run_id"]
    verdict = result["progress"]["objective_verdict"]
    evidence = verdict["evidence"][0]
    artifact = json.loads((run_dir / evidence["artifact_ref"]).read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["success"] is False
    assert result["failure_class"] == "verifier_unavailable"
    assert verdict["status"] == "unavailable"
    assert artifact["failure_type"] == "VerifierError"
    assert "private details" not in json.dumps(artifact)
    assert audit_run(run_dir).status == "consistent"


def test_environment_start_failure_is_classified_separately_from_solver_failure(
    monkeypatch, tmp_path: Path
) -> None:
    class FailingEnvironment:
        def start(self):
            raise RuntimeError("simulated startup failure api_key=private-canary-value")

        def stop(self, *, preserve_state=False):
            return None

    monkeypatch.setattr(
        "harness.orchestrator.build_environment", lambda *_args, **_kwargs: FailingEnvironment()
    )
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/demo.yaml")
    result = Orchestrator(tmp_path / "runs").run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=1,
    )

    assert result["success"] is False
    assert result["failure_class"] == "environment_failure"
    assert result["progress"]["objective_verdict"] is None
    run_dir = tmp_path / "runs" / result["run_id"]
    assert "private-canary-value" not in json.dumps(result)
    assert "private-canary-value" not in "".join(
        path.read_text(encoding="utf-8")
        for path in run_dir.iterdir()
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    )
def test_smoke_resume_world_state(tmp_path: Path) -> None:
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/demo.yaml")
    orchestrator = Orchestrator(tmp_path / "runs")

    first = orchestrator.run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=1,
    )
    first_dir = tmp_path / "runs" / first["run_id"]
    world_events = first_dir / "world.events.jsonl"
    world_context = first_dir / "world.context.txt"

    assert world_events.is_file()
    assert world_context.is_file()
    assert "Red Harness World Context" in world_context.read_text(encoding="utf-8")

    second = orchestrator.run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=2,
        resume_world_events=world_events,
    )

    assert second["world"]["resumed"] is True
    assert second["world"]["revision"] >= first["world"]["revision"]


def test_smoke_resume_sqlite_world_state(tmp_path: Path) -> None:
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/demo.yaml")
    orchestrator = Orchestrator(tmp_path / "runs")

    first = orchestrator.run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=3,
    )
    first_dir = tmp_path / "runs" / first["run_id"]
    world_db = first_dir / "world.db"
    assert world_db.is_file()

    second = orchestrator.run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        allow_host_agent=True,
        seed=4,
        resume_world_events=world_db,
    )

    assert second["world"]["resumed"] is True
    assert second["world"]["revision"] >= first["world"]["revision"]
    second_dir = tmp_path / "runs" / second["run_id"]
    assert (second_dir / "world.db").is_file()
class _ResumeSession:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    async def events(self):
        (self.run_dir / "proof.txt").write_text("red-harness-ok", encoding="utf-8")
        if False:
            yield AgentEvent(type="unused", data={})

    async def result(self):
        return AgentResult(0, False, None, "", "", UsageMetrics())

    async def checkpoint(self):
        return AgentCheckpoint("checkpoint-resume", 0, 0, "pi-session-existing")

    async def observe(self, _observation):
        return None


class _ResumeAdapter:
    def __init__(self) -> None:
        self.resume_session_id = None
        self.session_task = None

    async def start_session(self, context, **_kwargs):
        self.resume_session_id = context.resume_session_id
        self.session_task = context.task
        return _ResumeSession(context.run_dir)


def _prepare_incomplete_pi_run(
    runs_root: Path,
    task_path: Path,
    agent_path: Path,
    *,
    budget_used: dict | None = None,
):
    task = load_task(task_path)
    agent = load_agent(agent_path)
    run_dir = runs_root / "run_incomplete"
    run_dir.mkdir(parents=True)
    workspace = create_agent_workspace(run_dir)
    trace = TraceRecorder(run_dir / "trace.jsonl", run_dir.name, task.id)
    trace.emit("run.started", data={
        "agent_id": agent.id,
        "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
        "agent_sha256": hashlib.sha256(agent_path.read_bytes()).hexdigest(),
    })
    runtime = bootstrap_solver(
        run_dir=run_dir, trace=trace,
        goal=Goal(id=f"goal:{task.id}:objective", description=task.objective.description),
        actor=f"agent:{agent.id}", context_query=task.objective.description,
        budget_limits=task.budgets.model_dump(exclude_none=True),
        agent_workspace=workspace,
    )

    class _CheckpointSession:
        async def checkpoint(self):
            return AgentCheckpoint("checkpoint-existing", 0, 0, "pi-session-existing")

    asyncio.run(save_session_checkpoint(
        run_dir=run_dir, session=_CheckpointSession(),
        world_revision=runtime.world.snapshot.revision, progress=runtime.progress,
        budget_used=budget_used or {}, processed_event_ids={},
    ))
    return task, agent, run_dir
def test_orchestrator_resumes_pi_session_in_existing_run(monkeypatch, tmp_path: Path) -> None:
    import harness.orchestrator as orchestrator_module

    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/pi.yaml")
    runs_root = tmp_path / "runs"
    task, agent, run_dir = _prepare_incomplete_pi_run(
        runs_root,
        task_path,
        agent_path,
        budget_used={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9,
                     "model_calls": 1, "tool_calls": 2, "cost_usd": 0.01},
    )
    from harness.world import SQLiteWorldRepository

    world = SQLiteWorldRepository(run_dir / "world.events.jsonl")
    initial_revision = world.snapshot.revision
    fake = _ResumeAdapter()
    monkeypatch.setattr(orchestrator_module, "build_agent_adapter", lambda *_a, **_k: fake)

    result = Orchestrator(runs_root).run(
        task=task, task_path=task_path, agent=agent, agent_path=agent_path,
        resume_run_dir=run_dir,
    )

    assert result["run_id"] == run_dir.name
    assert result["success"] is True
    assert fake.resume_session_id == "pi-session-existing"
    assert fake.session_task.budgets.max_tokens == 91
    assert fake.session_task.budgets.max_tool_calls == 1
    assert result["metrics"]["total_tokens"] == 9
    assert result["metrics"]["tool_calls"] == 2
    assert result["world"]["revision"] > initial_revision
    goal_events = [event for event in world.events()
                   if event.actor == "harness" and event.kind == "goal"]
    assert len({event.object["id"] for event in goal_events}) == 1
