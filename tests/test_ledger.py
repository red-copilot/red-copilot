from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from harness.ledger import LedgerConflict, LedgerStore, shadow_ledger_enabled
from harness.trace import TraceRecorder


def _event(event_id: str, event_type: str, payload: dict, **extra: object) -> dict:
    return {
        "event_id": event_id,
        "run_id": "run-ledger",
        "type": event_type,
        "ts": "2026-10-22T12:00:00+00:00",
        "actor": "harness",
        "data": payload,
        **extra,
    }


def test_trace_recorder_append_only_chain_and_artifact_refs(tmp_path: Path) -> None:
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-ledger",
        "task-ledger",
        shadow_ledger=True,
    )
    trace.emit("run.started", data={"task": "ledger"})
    objective = trace.emit(
        "objective.verdict",
        data={
            "producer": "task_verifier",
            "status": "verified",
            "evidence": [
                {
                    "artifact_ref": "evidence/sha256/proof.json",
                    "artifact_hash": "a" * 64,
                }
            ],
        },
    )

    store = LedgerStore(tmp_path / "ledger.db")
    records = store.records()
    store.verify()

    assert [record.event_id for record in records] == [
        json.loads(line)["event_id"]
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert objective in {record.event_id for record in records}
    assert [record.ledger_seq for record in records] == [1, 2]
    assert records[1].producer == "task_verifier"
    assert records[1].trust_tier == "authorized"
    assert records[1].artifact_refs == ("evidence/sha256/proof.json",)
    assert records[0].prev_hash == "0" * 64
    assert records[1].prev_hash == records[0].record_hash

    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE ledger_records SET kind = 'tampered'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM ledger_records")
        connection.rollback()


def test_duplicate_event_id_with_different_content_fails_closed(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path / "ledger.db")
    event = _event("evt-1", "tool.call", {"tool": "bash"})
    store.append_trace_event(event)

    changed = _event("evt-1", "tool.result", {"is_error": True})
    with pytest.raises(LedgerConflict):
        store.append_trace_event(changed)

    assert len(store.records()) == 1


def test_ledger_failure_does_not_break_trace_or_run(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.db"
    ledger_path.mkdir()
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-ledger",
        "task-ledger",
        shadow_ledger=True,
        ledger_path=ledger_path,
    )

    trace.emit("run.started", data={"task": "ledger"})

    assert trace.ledger_error
    assert (tmp_path / "trace.jsonl").is_file()
    warning = json.loads((tmp_path / "ledger.error.json").read_text(encoding="utf-8"))
    assert warning["schema_version"] == "harness/ledger-shadow-error/v1"
    assert warning["error_type"]


def test_shadow_ledger_flag_disables_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_LEDGER_SHADOW", "0")
    assert shadow_ledger_enabled() is False
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-ledger", "task-ledger")

    trace.emit("run.started", data={"task": "ledger"})

    assert (tmp_path / "trace.jsonl").is_file()
    assert not (tmp_path / "ledger.db").exists()


FIXTURE_TRACE = Path(__file__).resolve().parent / "fixtures" / "run_metrics" / "trace_excerpt.jsonl"


def test_import_historical_trace_projects_actions_and_budget(tmp_path: Path) -> None:
    from harness.ledger import import_trace, project_records

    ledger_path = tmp_path / "ledger.db"
    records = import_trace(FIXTURE_TRACE, ledger_path)
    projection = project_records(records)

    assert len(records) == 4
    assert projection.run_id == "tsec_a-05_test0001"
    assert {action["tool_call_id"] for action in projection.actions} == {
        "call-slow",
        "call-fast",
    }
    assert projection.budget["tool_calls"] == 0
    assert projection.budget["tokens"] == 37820
    assert projection.terminal is None
    store = LedgerStore(ledger_path)
    store.verify()


def _write_complete_v2_run(run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = TraceRecorder(
        run_dir / "trace.jsonl",
        "run-complete",
        "task-ledger",
        shadow_ledger=True,
    )
    trace.emit(
        "tool.call",
        actor="agent:test",
        data={
            "tool": "bash",
            "tool_call_id": "action-1",
            "skill_id": "recon",
        },
        action_id="action-1",
    )
    trace.emit(
        "tool.result",
        actor="agent:test",
        data={
            "tool": "bash",
            "tool_call_id": "action-1",
            "skill_id": "recon",
            "is_error": False,
            "execution_status": "succeeded",
        },
        action_id="action-1",
    )
    trace.emit("model.request", actor="gateway", data={"count": 2})
    trace.emit(
        "model.usage",
        actor="gateway",
        data={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cost_usd": 0.5,
        },
    )
    for index, accepted in enumerate((True, False), start=1):
        trace.emit(
            "benchmark.submission.proposed",
            actor="agent",
            data={"value_sha256": f"value-{index}"},
        )
        trace.emit(
            "benchmark.submission.result",
            data={"accepted": accepted, "value_sha256": f"value-{index}"},
        )
    trace.emit(
        "objective.verdict",
        data={"producer": "task_verifier", "status": "verified"},
    )
    trace.emit(
        "run.finished",
        data={"status": "finished", "success": True, "score": 100.0},
    )
    (run_dir / "progress.json").write_text(
        json.dumps(
            {
                "budget_used": {"tokens": 120, "cost_usd": 0.5},
                "model_calls": 2,
                "tool_calls": 1,
                "last_action": {
                    "tool": "bash",
                    "tool_call_id": "action-1",
                    "status": "succeeded",
                    "skill_id": "recon",
                },
                "active_actions": {},
                "objective_verdict": {"status": "verified"},
            }
        ),
        encoding="utf-8",
    )
    result_path = run_dir / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "finished",
                "success": True,
                "score": 100.0,
                "metrics": {"submissions_attempted": 2},
                "submissions": [{"accepted": True}, {"accepted": False}],
            }
        ),
        encoding="utf-8",
    )
    return result_path


def test_complete_run_projection_matches_v2_and_is_deterministic(tmp_path: Path) -> None:
    from harness.ledger import compare_ledger_to_v2, project_records, write_ledger_comparison

    result_path = _write_complete_v2_run(tmp_path)
    ledger_path = tmp_path / "ledger.db"
    store = LedgerStore(ledger_path)
    projection = project_records(store.records())
    result = json.loads(result_path.read_text(encoding="utf-8"))
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))

    comparison = compare_ledger_to_v2(store.records(), result, progress)
    first = write_ledger_comparison(ledger_path, tmp_path)
    second = write_ledger_comparison(ledger_path, tmp_path)

    assert comparison.ok is True
    assert first["ok"] is True
    assert first["projection"] == second["projection"]
    assert projection.terminal == {"status": "finished", "success": True, "score": 100.0}
    assert projection.objective_status == "verified"
    assert projection.budget == {
        "tokens": 120,
        "model_calls": 2,
        "tool_calls": 1,
        "cost_usd": 0.5,
    }
    assert projection.submissions == {"proposed": 2, "accepted": 1, "rejected": 1}


def test_comparison_reports_terminal_mismatch_without_raising(tmp_path: Path) -> None:
    from harness.ledger import compare_ledger_to_v2, write_ledger_comparison

    result_path = _write_complete_v2_run(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "error"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    store = LedgerStore(tmp_path / "ledger.db")

    comparison = compare_ledger_to_v2(store.records(), result, progress)
    written = write_ledger_comparison(tmp_path / "ledger.db", tmp_path)

    assert comparison.ok is False
    assert written["ok"] is False
    assert any(
        mismatch["field"] == "terminal.status" for mismatch in comparison.mismatches
    )


def _prepare_persistence_case(
    run_dir: Path,
    *,
    progress_tokens: int = 120,
) -> tuple[TraceRecorder, dict]:
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = TraceRecorder(
        run_dir / "trace.jsonl",
        "run-persist",
        "task-ledger",
        shadow_ledger=True,
    )
    trace.emit("tool.call", actor="agent:test", data={"tool": "bash", "tool_call_id": "a1"})
    trace.emit(
        "tool.result",
        actor="agent:test",
        data={"tool": "bash", "tool_call_id": "a1", "execution_status": "succeeded"},
    )
    trace.emit("model.request", actor="gateway", data={"count": 2})
    trace.emit(
        "model.usage",
        actor="gateway",
        data={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )
    trace.emit("benchmark.submission.proposed", actor="agent", data={"value_sha256": "v1"})
    trace.emit("benchmark.submission.result", data={"accepted": True})
    trace.emit("objective.verdict", data={"producer": "task_verifier", "status": "verified"})
    (run_dir / "progress.json").write_text(
        json.dumps(
            {
                "budget_used": {"tokens": progress_tokens},
                "model_calls": 2,
                "tool_calls": 1,
                "last_action": {
                    "tool": "bash",
                    "tool_call_id": "a1",
                    "status": "succeeded",
                },
                "active_actions": {},
                "objective_verdict": {"status": "verified"},
            }
        ),
        encoding="utf-8",
    )
    return trace, {
        "status": "finished",
        "success": True,
        "score": 100.0,
        "metrics": {"submissions_attempted": 1},
    }


def test_persist_run_result_finalizes_shadow_comparison(tmp_path: Path) -> None:
    from harness.runtime.results import persist_run_result

    trace, result = _prepare_persistence_case(tmp_path)

    persist_run_result(run_dir=tmp_path, trace=trace, result=result)

    comparison = json.loads((tmp_path / "ledger.comparison.json").read_text(encoding="utf-8"))
    assert comparison["ok"] is True
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result


def test_persist_run_result_keeps_result_on_projection_mismatch(tmp_path: Path) -> None:
    from harness.runtime.results import persist_run_result

    trace, result = _prepare_persistence_case(tmp_path, progress_tokens=999)

    persist_run_result(run_dir=tmp_path, trace=trace, result=result)

    comparison = json.loads((tmp_path / "ledger.comparison.json").read_text(encoding="utf-8"))
    assert comparison["ok"] is False
    assert any(item["field"] == "budget.tokens" for item in comparison["mismatches"])
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result
