from __future__ import annotations

import pytest

from harness.kernel import LedgerAppender, SQLiteLedgerAppender
from harness.ledger import LedgerConflict


def _event(event_id: str, event_type: str = "run.started") -> dict:
    return {
        "event_id": event_id,
        "run_id": "run-kernel",
        "type": event_type,
        "ts": "2026-10-23T12:00:00+00:00",
        "actor": "harness",
        "data": {"task": "kernel"},
    }


def test_sqlite_appender_delegates_single_and_batch_appends(tmp_path) -> None:
    appender: LedgerAppender = SQLiteLedgerAppender(tmp_path / "ledger.db")

    first = appender.append_trace_event(_event("evt-1"))
    batch = appender.append_batch([_event("evt-2"), _event("evt-3")])
    appender.verify()

    assert first.ledger_seq == 1
    assert [record.ledger_seq for record in batch] == [2, 3]
    assert appender.path == tmp_path / "ledger.db"
    appender.close()


def test_sqlite_appender_preserves_batch_conflict_semantics(tmp_path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    appender.append_trace_event(_event("evt-1"))

    changed = _event("evt-1", event_type="tool.result")
    with pytest.raises(LedgerConflict):
        appender.append_batch([_event("evt-2"), changed])

    appender.verify()
    appender.close()


def test_sqlite_appender_exposes_records_in_sequence_order(tmp_path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    appender.append_trace_event(_event("evt-1"))
    appender.append_batch([_event("evt-2"), _event("evt-3")])

    records = appender.records()

    assert [record.ledger_seq for record in records] == [1, 2, 3]
    assert [record.event_id for record in records] == ["evt-1", "evt-2", "evt-3"]
    appender.close()
