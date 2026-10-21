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
