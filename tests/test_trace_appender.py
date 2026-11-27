from __future__ import annotations

import json
from pathlib import Path

from harness.kernel import SQLiteLedgerAppender
from harness.trace import TraceRecorder


class RecordingAppender:
    path = Path("unused-ledger.db")

    def __init__(self) -> None:
        self.events = []

    def append_trace_event(self, event):
        self.events.append(event)
        return object()

    def append_batch(self, events):
        self.events.extend(events)
        return ()

    def verify(self) -> None:
        return None

    def close(self) -> None:
        return None


class FailingAppender(RecordingAppender):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def append_batch(self, events):
        self.calls += 1
        raise RuntimeError("injected append failure")


def test_trace_recorder_uses_injected_appender(tmp_path: Path) -> None:
    appender = RecordingAppender()
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=True,
        ledger_appender=appender,
    )

    trace.emit("run.started", data={"task": "trace"})
    # Events are buffered and flushed in batches; an explicit flush makes
    # them visible to callers that need the ledger synchronously.
    assert appender.events == []

    trace.flush_shadow()

    assert len(appender.events) == 1
    assert appender.events[0]["type"] == "run.started"
    assert not (tmp_path / "ledger.db").exists()


def test_trace_recorder_disables_failed_injected_appender(tmp_path: Path) -> None:
    appender = FailingAppender()
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=True,
        ledger_appender=appender,
    )

    trace.emit("run.started", data={"first": True})
    trace.emit("model.request", data={"count": 1})

    trace.flush_shadow()

    assert appender.calls == 1
    assert trace.ledger_error is not None
    assert (tmp_path / "ledger.error.json").is_file()



def test_disabled_shadow_ignores_injected_appender(tmp_path: Path) -> None:
    appender = RecordingAppender()
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=False,
        ledger_appender=appender,
    )

    trace.emit("run.started", data={"task": "trace"})

    assert appender.events == []
    assert not (tmp_path / "ledger.db").exists()


def test_finalize_shadow_uses_injected_appender_path(tmp_path: Path) -> None:
    custom_path = tmp_path / "custom-ledger.db"
    appender = SQLiteLedgerAppender(custom_path)
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=True,
        ledger_path=tmp_path / "default-ledger.db",
        ledger_appender=appender,
    )
    trace.emit(
        "run.finished",
        data={"status": "finished", "success": True, "score": 0.0, "metrics": {}},
    )
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "success": True,
                "score": 0.0,
                "metrics": {"submissions_attempted": 0},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "progress.json").write_text(
        json.dumps(
            {
                "budget_used": {"tokens": 0, "cost_usd": 0},
                "model_calls": 0,
                "tool_calls": 0,
                "last_action": None,
                "active_actions": {},
                "objective_verdict": None,
            }
        ),
        encoding="utf-8",
    )

    comparison = trace.finalize_shadow(tmp_path)

    assert comparison is not None
    assert comparison["ok"] is True
    assert custom_path.is_file()
    assert not (tmp_path / "default-ledger.db").exists()


def test_trace_recorder_flushes_the_shadow_in_batches(tmp_path: Path) -> None:
    """Bounded buffering: auto-flush at the threshold, explicit flush before.

    The shadow ledger mirrored every trace event with its own transaction,
    which put several WAL fsyncs on every agent event. Events now buffer
    and drain in one atomic batch (64 by default); a clean run flushes at
    finalize, and a crash only ever loses the buffered tail of a ledger
    that is explicitly not a production source of truth.
    """
    import harness.trace as trace_module

    appender = RecordingAppender()
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=True,
        ledger_appender=appender,
    )
    threshold = trace_module.SHADOW_LEDGER_FLUSH_EVENTS

    for index in range(threshold):
        trace.emit("tool.call", data={"index": index})
    assert len(appender.events) == threshold  # auto-flushed at the threshold

    trace.emit("tool.call", data={"index": threshold})
    assert len(appender.events) == threshold  # one event pending
    trace.flush_shadow()
    assert len(appender.events) == threshold + 1

    # A failed flush disables the shadow instead of failing the run.
    trace.flush_shadow()  # empty buffer is a no-op, appender untouched
    assert appender.events == list(appender.events)


def test_finalize_shadow_flushes_the_buffered_tail(tmp_path: Path) -> None:
    custom_path = tmp_path / "custom-ledger.db"
    appender = SQLiteLedgerAppender(custom_path)
    trace = TraceRecorder(
        tmp_path / "trace.jsonl",
        "run-trace",
        "task-trace",
        shadow_ledger=True,
        ledger_path=tmp_path / "default-ledger.db",
        ledger_appender=appender,
    )
    trace.emit("run.started", data={"task": "trace"})
    trace.emit(
        "run.finished",
        data={"status": "finished", "success": True, "score": 0.0, "metrics": {}},
    )
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "success": True,
                "score": 0.0,
                "metrics": {"submissions_attempted": 0},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "progress.json").write_text(
        json.dumps(
            {
                "budget_used": {"tokens": 0, "cost_usd": 0},
                "model_calls": 0,
                "tool_calls": 0,
                "last_action": None,
                "active_actions": {},
                "objective_verdict": None,
            }
        ),
        encoding="utf-8",
    )

    comparison = trace.finalize_shadow(tmp_path)

    assert comparison is not None
    assert comparison["ok"] is True
    # Both buffered events reached the ledger before verify+compare ran.
    assert comparison["projection"]["event_count"] == 2
