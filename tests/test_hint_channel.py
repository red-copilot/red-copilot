from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app
from harness.hints import HintCursor, append_hint
from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.solver_loop import SolverLoop
from harness.trace import TraceRecorder
from harness.world import SQLiteWorldRepository


class FakeSession:
    def __init__(self) -> None:
        self.feedback = []

    async def observe(self, observation) -> None:
        self.feedback.append(observation)


def _loop(tmp_path: Path) -> tuple[SolverLoop, FakeSession, ProgressLedger]:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger(budget_limits={"wall_time": 3600})
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-hints", "task-hints")
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
    return loop, session, progress


def test_append_hint_writes_sequential_records(tmp_path: Path) -> None:
    first = append_hint(tmp_path, "check the admin panel first")
    second = append_hint(tmp_path, "the flag format is zc{...}")

    assert first.id == "hint_0001"
    assert second.id == "hint_0002"
    lines = (tmp_path / "hints.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert payloads[0]["text"] == "check the admin panel first"
    assert payloads[1]["text"] == "the flag format is zc{...}"


def test_hint_cursor_polls_incrementally_and_skips_malformed_lines(tmp_path: Path) -> None:
    append_hint(tmp_path, "hint one")
    cursor = HintCursor(tmp_path / "hints.jsonl")

    first = cursor.poll()
    assert [hint.text for hint in first] == ["hint one"]

    (tmp_path / "hints.jsonl").open("a", encoding="utf-8").write("not json\n\n")
    append_hint(tmp_path, "hint two")

    second = cursor.poll()
    assert [hint.text for hint in second] == ["hint two"]
    # Re-polling without new content yields nothing new.
    assert cursor.poll() == []


def test_solver_loop_delivers_hint_once_and_upgrades_planning(tmp_path: Path) -> None:
    loop, session, progress = _loop(tmp_path)
    append_hint(tmp_path, "try the backup host on port 8443")
    usage = AgentEvent(type="model.usage", data={"total_tokens": 1, "cost_usd": 0.0})

    asyncio.run(loop.process_event(session, usage))

    delivered = [item for item in session.feedback if item.type == "solver.hint"]
    assert len(delivered) == 1
    assert "8443" in delivered[0].data["message"]
    observation = loop.world.snapshot.observations.get("human.hint:hint_0001")
    assert observation is not None
    assert observation.provenance.epistemic_status == "evidence"
    # The hint replans within the same event, so the reason is consumed by the
    # plan publication rather than lingering in the ledger.
    assert progress.replan_count >= 1
    assert any(item.type == "solver.plan.updated" for item in session.feedback)
    trace_rows = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["type"] == "solver.hint" for row in trace_rows)

    # A later event must not redeliver the same hint.
    asyncio.run(loop.process_event(session, usage))
    assert len([item for item in session.feedback if item.type == "solver.hint"]) == 1


def test_finish_ingest_harvests_late_hints_into_world(tmp_path: Path) -> None:
    loop, session, _progress = _loop(tmp_path)
    asyncio.run(loop.process_event(session, AgentEvent(type="model.usage", data={})))
    append_hint(tmp_path, "late operator nudge")

    loop.finish_ingest()

    observation = loop.world.snapshot.observations.get("human.hint:hint_0001")
    assert observation is not None
    assert observation.content["text"] == "late operator nudge"


def test_hint_cli_appends_and_rejects_bad_directories(tmp_path: Path) -> None:
    runner = CliRunner()
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()

    ok = runner.invoke(app, ["hint", str(run_dir), "--text", "focus on the login flow"])
    assert ok.exit_code == 0
    assert json.loads(ok.output)["id"] == "hint_0001"

    stdin = runner.invoke(app, ["hint", str(run_dir)], input="multi\nline\nhint\n")
    assert stdin.exit_code == 0
    payloads = [
        json.loads(line)
        for line in (run_dir / "hints.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert payloads[1]["text"] == "multi\nline\nhint"

    missing = runner.invoke(app, ["hint", str(tmp_path / "nope"), "--text", "x"])
    assert missing.exit_code != 0


def test_hint_observation_appears_in_run_summary(tmp_path: Path) -> None:
    from harness.runtime.projections import run_summary

    loop, session, _progress = _loop(tmp_path)
    append_hint(tmp_path, "summary-visible hint")
    asyncio.run(loop.process_event(session, AgentEvent(type="model.usage", data={})))

    summary = run_summary(
        world=loop.world,
        progress=_progress,
        run_dir=tmp_path,
        outcome={"status": "finished", "success": False, "failure_class": "solver_failure"},
    )
    hint_facts = [fact for fact in summary["facts"] if fact["id"] == "human.hint:hint_0001"]
    assert hint_facts and hint_facts[0]["trust"] == "evidence"
    assert (tmp_path / "summary.json").is_file()


def test_rebuilt_loop_does_not_redeliver_absorbed_hints(tmp_path: Path) -> None:
    """A resumed run rebuilds SolverLoop with a fresh cursor offset; hints the
    World already absorbed must not be re-steered or re-replanned."""
    loop, session, _progress = _loop(tmp_path)
    append_hint(tmp_path, "already absorbed")
    asyncio.run(loop.process_event(session, AgentEvent(type="model.usage", data={})))
    assert len([item for item in session.feedback if item.type == "solver.hint"]) == 1
    first_replans = _progress.replan_count if hasattr(_progress, "replan_count") else 0
    del first_replans

    # Simulate a resume: same world, same hints.jsonl, brand-new cursor state.
    resumed_progress = ProgressLedger(budget_limits={"wall_time": 3600})
    resumed_trace = TraceRecorder(tmp_path / "trace.jsonl", "run-hints", "task-hints")
    resumed_session = FakeSession()
    resumed_loop = SolverLoop(
        world=loop.world,
        progress=resumed_progress,
        run_dir=tmp_path,
        trace=resumed_trace,
        actor="agent:test",
        context_query="solve the target",
        planned_world_revision=loop.world.snapshot.revision,
        skills=[],
    )
    asyncio.run(
        resumed_loop.process_event(resumed_session, AgentEvent(type="model.usage", data={}))
    )

    assert resumed_session.feedback == [], "absorbed hint was redelivered after resume"


def test_append_hint_repairs_missing_trailing_newline(tmp_path: Path) -> None:
    # Simulate a hand-edited inbox whose last line lost its newline.
    inbox = tmp_path / "hints.jsonl"
    inbox.write_text('{"id":"hint_0001","text":"first","created_at":"t"}', encoding="utf-8")

    second = append_hint(tmp_path, "second")

    assert second.id == "hint_0002"
    lines = inbox.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "first"
    assert json.loads(lines[1])["text"] == "second"
