from __future__ import annotations

from pathlib import Path

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

    def append_trace_event(self, event):
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

    assert appender.calls == 1
    assert trace.ledger_error is not None
    assert (tmp_path / "ledger.error.json").is_file()
