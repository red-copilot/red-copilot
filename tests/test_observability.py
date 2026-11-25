from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from harness.gateway import create_gateway_app
from harness.models import load_agent, load_task
from harness.orchestrator import Orchestrator
from harness.progress import ProgressLedger
from harness.runtime.checkpoint import (
    FileCheckpointStore,
    RunCheckpoint,
    load_recovery_state,
)
from harness.session import AgentEvent
from harness.solver_loop import SolverLoop
from harness.trace import TraceRecorder
from harness.world import SQLiteWorldRepository


def test_run_setup_failure_is_logged_without_exception_text(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    def failing_bootstrap(**_kwargs):
        raise RuntimeError("bootstrap exploded with api_key=private-canary")

    monkeypatch.setattr("harness.orchestrator.bootstrap_solver", failing_bootstrap)
    caplog.set_level(logging.ERROR, logger="harness.orchestrator")
    task_path = Path("benchmarks/examples/hello/task.yaml")
    agent_path = Path("agents/examples/pi.yaml")

    Orchestrator(tmp_path / "runs").run(
        task=load_task(task_path),
        task_path=task_path,
        agent=load_agent(agent_path),
        agent_path=agent_path,
        seed=3,
        run_id="run_logged_setup_failure",
    )

    assert "run setup failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "private-canary" not in caplog.text


def test_gateway_upstream_error_is_logged(tmp_path: Path, caplog) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = create_gateway_app(
        event_file=tmp_path / "events.jsonl",
        workspace=tmp_path,
        task_dir=tmp_path,
        gateway_token="gateway-secret",
        model_upstream="https://model.example/v1",
        http_transport=httpx.MockTransport(upstream),
    )
    caplog.set_level(logging.WARNING, logger="harness.gateway")

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"model": "m", "messages": []},
    )

    assert response.status_code == 502
    assert "gateway upstream error" in caplog.text
    assert "error_type=ConnectError" in caplog.text


def test_recovery_skew_is_logged(tmp_path: Path, caplog) -> None:
    run_dir = tmp_path / "skewed-run"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(
            run_id="skewed-run",
            world_revision=1,
            event_offset=3,
            feedback_offset=0,
            progress_event_count=4,
            processed_event_ids={
                "event-1": "h1",
                "event-2": "h2",
                "event-3": "h3",
                "event-4": "h4",
            },
        )
    )
    ProgressLedger(event_count=5, last_event_id="event-5").write(
        run_dir / "progress.json"
    )
    caplog.set_level(logging.WARNING, logger="harness.runtime.checkpoint")

    load_recovery_state(run_dir)

    assert "recovery progress skew" in caplog.text
    assert "checkpoint=4 progress=5" in caplog.text


def test_aci_rejection_is_logged(tmp_path: Path, caplog) -> None:
    class Session:
        async def observe(self, _observation) -> None:
            return None

        async def close(self, _reason) -> None:
            return None

    world = SQLiteWorldRepository(tmp_path / "world.db")
    loop = SolverLoop(
        world=world,
        progress=ProgressLedger(),
        run_dir=tmp_path,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_aci_log", "task"),
        actor="agent:test",
        context_query="aci logging",
        planned_world_revision=world.snapshot.revision,
    )
    caplog.set_level(logging.WARNING, logger="harness.solver_loop")

    asyncio.run(
        loop.process_event(
            Session(),
            AgentEvent(type="world.hypothesis", data={}, event_id="bad-1"),
        )
    )

    assert "aci event rejected" in caplog.text
    assert "event_type=world.hypothesis" in caplog.text
    assert "error_type=ValidationError" in caplog.text
