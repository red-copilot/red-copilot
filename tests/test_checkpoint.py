import asyncio
import errno
import json
import os
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from harness.progress import ProgressLedger
from harness.runtime.checkpoint import (
    FileCheckpointStore,
    RunCheckpoint,
    load_recovery_state,
    reconcile_pending_actions,
    save_session_checkpoint,
)
from harness.session import AgentCheckpoint


def test_checkpoint_roundtrip_and_replace(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path / "run" / "checkpoint.json")
    assert store.load() is None
    initial = RunCheckpoint(
        run_id="run-a",
        world_revision=3,
        event_offset=12,
        feedback_offset=4,
        budget_used={"tokens": 400.0},
        pending_actions=[{"tool_call_id": "tool-1", "status": "unknown"}],
        agent_session_id="session-a",
    )
    store.save(initial)
    assert store.load() == initial
    store.save(initial.model_copy(update={"event_offset": 20}))
    assert store.load().event_offset == 20


def test_checkpoint_refuses_unrecoverable_oversized_payload_before_replacing(
    tmp_path: Path,
) -> None:
    store = FileCheckpointStore(tmp_path / "checkpoint.json")
    initial = RunCheckpoint(
        run_id="bounded-checkpoint",
        world_revision=0,
        event_offset=0,
        feedback_offset=0,
    )
    store.save(initial)
    original = store.path.read_bytes()
    large = initial.model_copy(
        update={
            "processed_event_ids": {
                f"event-{index:06d}": "a" * 64 for index in range(100_000)
            }
        }
    )

    with pytest.raises(ValueError, match="8 MiB recovery limit"):
        store.save(large)

    assert store.path.read_bytes() == original
    assert store.load() == initial
    assert not list(tmp_path.glob(".checkpoint.json.*.tmp"))


def test_session_checkpoint_keeps_recent_event_dedupe_ids_within_read_limit(
    tmp_path: Path,
) -> None:
    class Session:
        async def checkpoint(self):
            return AgentCheckpoint(
                id="checkpoint-large-map",
                event_offset=100_000,
                feedback_offset=0,
            )

    class Progress:
        active_actions: ClassVar[dict] = {}
        last_action = None
        event_count = 100_000

    processed = {
        f"agent-event-{index:06d}": "a" * 64 for index in range(100_000)
    }
    record = asyncio.run(
        save_session_checkpoint(
            run_dir=tmp_path,
            session=Session(),
            world_revision=1,
            progress=Progress(),
            processed_event_ids=processed,
        )
    )

    assert record is not None
    assert len(record.processed_event_ids) < len(processed)
    assert "agent-event-099999" in record.processed_event_ids
    assert "agent-event-000000" not in record.processed_event_ids
    assert FileCheckpointStore(tmp_path / "checkpoint.json").load() == record
    assert not list((tmp_path / "run").glob("*.tmp"))


def test_checkpoint_save_failure_keeps_previous_record(tmp_path: Path, monkeypatch) -> None:
    store = FileCheckpointStore(tmp_path / "checkpoint.json")
    initial = RunCheckpoint(run_id="run-a", world_revision=1, event_offset=3, feedback_offset=0)
    store.save(initial)

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr("harness.runtime.checkpoint.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        store.save(initial.model_copy(update={"event_offset": 4}))

    assert store.load() == initial
    assert not list(tmp_path.glob("*.tmp"))


def test_checkpoint_disk_full_during_sync_preserves_previous_record(
    tmp_path: Path, monkeypatch
) -> None:
    store = FileCheckpointStore(tmp_path / "checkpoint.json")
    initial = RunCheckpoint(run_id="run-a", world_revision=1, event_offset=3, feedback_offset=0)
    store.save(initial)

    def disk_full(_descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr("harness.runtime.checkpoint.os.fsync", disk_full)
    with pytest.raises(OSError) as error:
        store.save(initial.model_copy(update={"event_offset": 4}))

    assert error.value.errno == errno.ENOSPC
    assert store.load() == initial
    assert not list(tmp_path.glob("*.tmp"))
def test_progress_ledger_write_is_atomic_on_replace_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "progress.json"
    prior = ProgressLedger(verified_actions=1)
    prior.write(path)
    original = path.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise OSError("injected progress replace failure")

    monkeypatch.setattr("harness.progress.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected progress"):
        ProgressLedger(verified_actions=2).write(path)

    assert path.read_bytes() == original
    assert ProgressLedger.model_validate_json(path.read_bytes()).verified_actions == 1
    assert not list(tmp_path.glob(".progress.json.*.tmp"))
@pytest.mark.parametrize(
    "values",
    [
        {"schema_version": "harness/checkpoint/v0"},
        {"budget_used": {"tokens": float("nan")}},
        {"budget_used": {"tokens": -1}},
        {"budget_used": {"tokens": "12"}},
        {"budget_used": {"tokens": True}},
        {"budget_used": {"total_tokens": 3.0}},
        {"budget_used": {"tool_calls": 3.9}},
        {
            "pending_actions": [
                {"tool_call_id": "same", "status": "running"},
                {"tool_call_id": "same", "status": "running"},
            ]
        },
    ],
)
def test_checkpoint_rejects_invalid_recovery_fields(values: dict) -> None:
    with pytest.raises(ValidationError):
        RunCheckpoint.model_validate(
            {
                "run_id": "run-a",
                "world_revision": 0,
                "event_offset": 0,
                "feedback_offset": 0,
                **values,
            }
        )
def test_checkpoint_v1_pending_action_id_migrates_to_v2(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "harness/checkpoint/v1",
                "run_id": "legacy-run",
                "world_revision": 1,
                "event_offset": 2,
                "feedback_offset": 0,
                "pending_actions": [{"id": "legacy-call", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )

    migrated = FileCheckpointStore(path).load()

    assert migrated.schema_version == "harness/checkpoint/v2"
    assert migrated.pending_actions[0]["tool_call_id"] == "legacy-call"


def test_completed_session_snapshot(tmp_path: Path) -> None:
    class Session:
        async def checkpoint(self):
            return AgentCheckpoint(
                id="cp-3",
                event_offset=24,
                feedback_offset=9,
                session_id="pi-session-3",
                gateway_event_offset=31,
            )

    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    ledger = ProgressLedger(last_action={"tool_call_id": "a", "status": "running"})
    result = asyncio.run(
        save_session_checkpoint(
            run_dir=run_dir,
            session=Session(),
            world_revision=4,
            progress=ledger,
            budget_used={"total_tokens": 20, "cost_usd": 0.03},
            processed_event_ids={"event-1": "hash-1"},
        )
    )
    assert result is not None
    assert result.session_checkpoint_id == "cp-3"
    assert result.agent_session_id == "pi-session-3"
    assert result.gateway_event_offset == 31
    assert result.pending_actions[0]["tool_call_id"] == "a"
    assert result.budget_used == {"total_tokens": 20, "cost_usd": 0.03}
    assert result.processed_event_ids == {"event-1": "hash-1"}
    assert FileCheckpointStore(run_dir / "checkpoint.json").load() == result


def test_recovery_restores_progress(tmp_path: Path) -> None:
    run_dir = tmp_path / "safe-run"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(run_id="safe-run", world_revision=2, event_offset=19, feedback_offset=0)
    )
    ProgressLedger(verified_actions=2).write(run_dir / "progress.json")
    checkpoint, progress = load_recovery_state(run_dir)
    assert checkpoint.event_offset == 19
    assert progress.verified_actions == 2


def test_recovery_refuses_unresolved_actions(tmp_path: Path) -> None:
    run_dir = tmp_path / "interrupted-run"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(
            run_id="interrupted-run",
            world_revision=1,
            event_offset=0,
            feedback_offset=0,
            pending_actions=[{"tool_call_id": "call-1", "status": "running"}],
        )
    )
    ProgressLedger().write(run_dir / "progress.json")
    with pytest.raises(RuntimeError, match="unresolved actions"):
        load_recovery_state(run_dir)
def test_pending_action_reconciliation_is_explicit_and_idempotent(tmp_path: Path) -> None:
    run_dir = tmp_path / "reconcile-run"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(
            run_id="reconcile-run",
            world_revision=2,
            event_offset=5,
            feedback_offset=1,
            pending_actions=[
                {"tool_call_id": "call-1", "status": "running"},
                {"tool_call_id": "call-2", "status": "running"},
            ],
        )
    )
    ProgressLedger(
        last_action={"tool_call_id": "call-2", "status": "running"},
        active_actions={
            "call-1": {"tool_call_id": "call-1", "status": "running"},
            "call-2": {"tool_call_id": "call-2", "status": "running"},
        },
    ).write(run_dir / "progress.json")

    outcomes = {"call-1": "succeeded", "call-2": "unknown"}
    first = reconcile_pending_actions(run_dir, outcomes=outcomes)
    checkpoint = FileCheckpointStore(run_dir / "checkpoint.json").load()
    with pytest.raises(RuntimeError, match="unknown action effects"):
        load_recovery_state(run_dir)
    assert checkpoint.pending_actions == [
        {"tool_call_id": "call-2", "status": "unknown"}
    ]
    second = reconcile_pending_actions(run_dir, outcomes={"call-2": "unknown"})
    resolved = reconcile_pending_actions(run_dir, outcomes={"call-2": "succeeded"})
    _, progress = load_recovery_state(run_dir)

    assert first.outcomes == outcomes
    assert second.outcomes == {"call-2": "unknown"}
    assert resolved.outcomes == {"call-2": "succeeded"}
    assert FileCheckpointStore(run_dir / "checkpoint.json").load().pending_actions == []
    assert progress.last_action["status"] == "succeeded"
    assert progress.execution_succeeded == 2
    assert progress.verified_actions == 0
    assert progress.reconciled_actions == {"call-1": "succeeded", "call-2": "succeeded"}
    assert (run_dir / "action.reconciliation.json").is_file()
@pytest.mark.skipif(os.name != "posix", reason="POSIX file locking is required")
def test_recovery_waits_for_reconciliation_to_publish_all_records(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "reconcile-recovery-race"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(
            run_id=run_dir.name,
            world_revision=0,
            event_offset=1,
            feedback_offset=0,
            pending_actions=[{"tool_call_id": "call-race", "status": "running"}],
        )
    )
    ProgressLedger(last_action={"tool_call_id": "call-race", "status": "running"}).write(
        run_dir / "progress.json"
    )

    progress_published = threading.Event()
    release_reconciler = threading.Event()
    recovery_finished = threading.Event()
    original_write = ProgressLedger.write
    recovery: list[tuple] = []

    def delayed_write(self, path):
        original_write(self, path)
        if Path(path).name == "progress.json":
            progress_published.set()
            assert release_reconciler.wait(timeout=5)

    def reconcile() -> None:
        reconcile_pending_actions(run_dir, outcomes={"call-race": "succeeded"})

    def recover() -> None:
        recovery.append(load_recovery_state(run_dir))
        recovery_finished.set()

    monkeypatch.setattr(ProgressLedger, "write", delayed_write)
    reconciliation_thread = threading.Thread(target=reconcile)
    reconciliation_thread.start()
    assert progress_published.wait(timeout=5)

    recovery_thread = threading.Thread(target=recover)
    recovery_thread.start()
    assert not recovery_finished.wait(timeout=0.1)

    release_reconciler.set()
    reconciliation_thread.join(timeout=5)
    recovery_thread.join(timeout=5)

    assert not reconciliation_thread.is_alive() and not recovery_thread.is_alive()
    assert recovery_finished.is_set()
    checkpoint, progress = recovery[0]
    assert checkpoint.pending_actions == []
    assert progress.reconciled_actions == {"call-race": "succeeded"}


def test_recovery_rejects_stale_world_revision(tmp_path: Path) -> None:
    run_dir = tmp_path / "revision-run"
    run_dir.mkdir()
    FileCheckpointStore(run_dir / "checkpoint.json").save(
        RunCheckpoint(run_id="revision-run", world_revision=3, event_offset=0, feedback_offset=0)
    )
    ProgressLedger().write(run_dir / "progress.json")
    with pytest.raises(ValueError, match="world revision mismatch"):
        load_recovery_state(run_dir, current_world_revision=4)
    record, _ = load_recovery_state(run_dir, current_world_revision=3)
    assert record.world_revision == 3
