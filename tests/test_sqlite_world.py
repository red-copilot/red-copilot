from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness.world import (
    Goal,
    Observation,
    SQLiteWorldRepository,
    WorldConflictError,
    WorldEvent,
)


def test_sqlite_world_repository_is_transactional_and_reopenable(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.events.jsonl")

    repository.upsert(
        "observation",
        Observation(
            id="obs-1",
            type="network.service",
            content={"port": 443, "service": "https"},
        ),
        actor="agent:test",
    )
    repository.upsert(
        "goal",
        Goal(id="goal-1", description="finish task", status="active"),
    )

    assert repository.snapshot.revision == 2
    assert (tmp_path / "world.db").is_file()
    repository.flush()
    assert (tmp_path / "world.events.jsonl").is_file()
    assert (tmp_path / "world.snapshot.json").is_file()

    reopened = SQLiteWorldRepository(tmp_path / "world.db")
    assert reopened.snapshot == repository.snapshot

    with sqlite3.connect(tmp_path / "world.db") as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        count = connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        assert count == 2
        object_count = connection.execute(
            "SELECT COUNT(*) FROM world_objects"
        ).fetchone()[0]
        assert object_count == 2


def test_sqlite_world_repository_deduplicates_event_ids(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    event = WorldEvent(
        id="wevt-fixed",
        kind="goal",
        object={"id": "goal-1", "description": "demo"},
    )

    first = repository.append(event, expected_revision=0)
    second = repository.append(event, expected_revision=0)

    assert first.revision == 1
    assert second.revision == 1
    assert len(repository.events()) == 1

    conflicting = event.model_copy(
        update={"object": {"id": "goal-1", "description": "changed"}}
    )
    with pytest.raises(ValueError, match="different payload"):
        repository.append(conflicting)


def test_sqlite_append_keeps_prior_returned_snapshot_stable(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    first = repository.upsert(
        "observation",
        Observation(id="obs-stable", type="test", content={"value": "before"}),
    )

    repository.upsert(
        "observation",
        Observation(id="obs-stable", type="test", content={"value": "after"}),
    )

    assert first.observations["obs-stable"].content["value"] == "before"
    assert repository.snapshot.observations["obs-stable"].content["value"] == "after"


def test_sqlite_snapshot_reader_observes_one_atomic_revision(
    tmp_path, monkeypatch
) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    revision_read = threading.Event()
    continue_reader = threading.Event()
    original_revision = repository._current_revision

    def pause_after_revision(connection):
        revision = original_revision(connection)
        if threading.current_thread().name.startswith("snapshot-reader"):
            revision_read.set()
            assert continue_reader.wait(timeout=5)
        return revision

    monkeypatch.setattr(repository, "_current_revision", pause_after_revision)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-reader") as pool:
        pending_snapshot = pool.submit(lambda: repository.snapshot)
        assert revision_read.wait(timeout=5)
        repository.upsert(
            "observation",
            Observation(id="obs-race", type="test", content={"value": 1}),
        )
        continue_reader.set()
        snapshot = pending_snapshot.result(timeout=5)

    assert snapshot.revision == 0
    assert snapshot.observations == {}


def test_sqlite_world_repository_supports_expected_revision(tmp_path) -> None:
    path = tmp_path / "world.db"
    first = SQLiteWorldRepository(path)
    second = SQLiteWorldRepository(path)

    first.upsert(
        "goal",
        {"id": "goal-1", "description": "first"},
        expected_revision=0,
    )

    with pytest.raises(WorldConflictError, match="expected 0, current 1"):
        second.upsert(
            "goal",
            {"id": "goal-2", "description": "stale writer"},
            expected_revision=0,
        )

    second.upsert(
        "goal",
        {"id": "goal-2", "description": "fresh writer"},
        expected_revision=1,
    )
    assert first.snapshot.revision == 2
    assert set(first.snapshot.goals) == {"goal-1", "goal-2"}


def test_sqlite_world_repository_persists_sequence_cursor(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    for index in range(3):
        repository.upsert(
            "goal",
            {"id": f"goal-{index}", "description": f"goal {index}"},
        )

    sequenced = repository.sequenced_events(after_sequence=1)

    assert [sequence for sequence, _event in sequenced] == [2, 3]
    assert [event.object["id"] for _sequence, event in sequenced] == [
        "goal-1",
        "goal-2",
    ]
    assert [event.object["id"] for event in repository.events(after_sequence=2)] == [
        "goal-2"
    ]


def test_sqlite_world_repository_imports_legacy_jsonl(tmp_path) -> None:
    event_path = tmp_path / "world.events.jsonl"
    events = [
        WorldEvent(
            id="wevt-1",
            kind="goal",
            object={"id": "goal-1", "description": "legacy"},
        ),
        WorldEvent(
            id="wevt-2",
            kind="observation",
            object={
                "id": "obs-1",
                "type": "legacy.fact",
                "content": {"value": 1},
            },
        ),
    ]
    event_path.write_text(
        "".join(event.model_dump_json() + "\n" for event in events),
        encoding="utf-8",
    )

    repository = SQLiteWorldRepository(event_path)

    assert repository.snapshot.revision == 2
    assert repository.snapshot.goals["goal-1"].description == "legacy"
    assert repository.snapshot.observations["obs-1"].content["value"] == 1

    exported = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [item["id"] for item in exported] == ["wevt-1", "wevt-2"]


def test_sqlite_world_imports_legacy_jsonl_idempotently(tmp_path) -> None:
    event_path = tmp_path / "world.events.jsonl"
    event_path.write_text(
        WorldEvent(
            id="wevt-1",
            kind="goal",
            object={"id": "goal-1", "description": "legacy"},
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    first = SQLiteWorldRepository(event_path)
    second = SQLiteWorldRepository(event_path)

    assert first.snapshot.revision == 1
    assert second.snapshot.revision == 1
    assert list(second.snapshot.goals) == ["goal-1"]


def test_sqlite_world_rejects_a_malformed_legacy_file_before_importing(tmp_path) -> None:
    event_path = tmp_path / "world.events.jsonl"
    event_path.write_text(
        WorldEvent(
            id="wevt-1",
            kind="goal",
            object={"id": "goal-1", "description": "legacy"},
        ).model_dump_json()
        + "\nnot-json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        SQLiteWorldRepository(event_path)

    with sqlite3.connect(tmp_path / "world.db") as connection:
        count = connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
    assert count == 0


def test_sqlite_world_uses_full_synchronous(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")

    with repository._connect() as connection:
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert synchronous == 2  # FULL


def test_sqlite_world_flush_fsyncs_both_exports_and_the_directory(
    tmp_path, monkeypatch
) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    repository.upsert("goal", Goal(id="goal-1", description="durable"))
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        "harness.world.sqlite.os.fsync",
        lambda descriptor: calls.append(descriptor) or real_fsync(descriptor),
    )

    repository.flush()

    # Two temporary files are made durable, then the publish directory is
    # fsynced so the renames survive a crash.
    assert len(calls) >= 3


def test_concurrent_snapshot_reads_never_tear_a_committed_revision(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    stop = threading.Event()
    failures: list[str] = []

    def read() -> None:
        last = 0
        while not stop.is_set():
            snapshot = repository.snapshot
            if snapshot.revision < last:
                failures.append("revision regressed")
            last = snapshot.revision
            if len(snapshot.observations) != snapshot.revision:
                failures.append("torn snapshot")

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for index in range(50):
            repository.upsert(
                "observation",
                Observation(
                    id=f"obs-{index}",
                    type="synthetic",
                    content={"index": index},
                ),
            )
    finally:
        stop.set()
        reader.join()

    assert failures == []


def test_sqlite_world_repository_serializes_concurrent_writers(tmp_path) -> None:
    path = tmp_path / "world.db"

    def write(index: int) -> None:
        repository = SQLiteWorldRepository(path)
        repository.upsert(
            "goal",
            {"id": f"goal-{index}", "description": f"goal {index}"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))

    repository = SQLiteWorldRepository(path)
    assert repository.snapshot.revision == 8
    assert len(repository.snapshot.goals) == 8
    assert [sequence for sequence, _event in repository.sequenced_events()] == list(
        range(1, 9)
    )


def test_sqlite_world_periodic_snapshot_flush_and_backup_restore(tmp_path) -> None:
    source = SQLiteWorldRepository(tmp_path / "world.db", snapshot_interval=4)
    for index in range(3):
        source.upsert(
            "observation",
            Observation(id=f"obs-{index}", type="synthetic", content={"index": index}),
        )
    assert not (tmp_path / "world.snapshot.json").exists()

    source.upsert(
        "observation",
        Observation(id="obs-3", type="synthetic", content={"index": 3}),
    )
    periodic = json.loads((tmp_path / "world.snapshot.json").read_text())
    assert periodic["revision"] == 4

    source.upsert(
        "observation",
        Observation(id="obs-4", type="synthetic", content={"index": 4}),
    )
    source.flush()
    flushed = json.loads((tmp_path / "world.snapshot.json").read_text())
    assert flushed["revision"] == 5

    compacted = source.compact()
    assert compacted["ok"] is True
    assert compacted["event_count"] == 5

    backup = source.backup_to(tmp_path / "restored" / "world.db")
    restored = SQLiteWorldRepository(backup)
    assert restored.snapshot == source.snapshot
    assert restored.replay() == source.replay()
    assert restored.integrity_check()["ok"] is True


def test_sqlite_world_integrity_check_detects_corrupt_snapshot_export(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    repository.upsert("goal", Goal(id="goal-1", description="preserve provenance"))
    repository.flush()
    repository.snapshot_path.write_text("{broken", encoding="utf-8")

    report = repository.integrity_check()

    assert report["ok"] is False
    assert "snapshot_corrupt" in report["issues"]


def test_sqlite_world_large_synthetic_append_budget(tmp_path) -> None:
    repository = SQLiteWorldRepository(tmp_path / "world.db")
    tracemalloc.start()
    started = time.monotonic()
    for index in range(1000):
        repository.upsert(
            "observation",
            Observation(
                id=f"synthetic-{index}",
                type="synthetic.large-run",
                content={"index": index, "payload": "x" * 128},
            ),
        )
    elapsed = time.monotonic() - started
    _current_bytes, peak_python_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stored_bytes = sum(
        path.stat().st_size
        for path in tmp_path.iterdir()
        if path.is_file() and path.name != "trace.jsonl"
    )

    assert repository.snapshot.revision == 1000
    # Branch-coverage instrumentation roughly triples this loop (measured 2.4s
    # plain, 8.4s under coverage), so the bound guards against algorithmic
    # regressions rather than a tight wall clock on a shared runner.
    assert elapsed < 30, f"append loop took {elapsed:.2f}s"
    assert peak_python_bytes < 32 * 1024 * 1024, f"peak={peak_python_bytes}"
    assert stored_bytes < 20 * 1024 * 1024


def test_snapshot_reads_are_served_from_the_revision_cache(tmp_path) -> None:
    """Unchanged-revision snapshot reads must not reload the world.

    The SolverLoop re-renders its context projection on every agent event,
    and each render used to pay a full SQLite load plus a pydantic
    revalidation of every materialized record just to observe a world that
    had not changed. The append path already maintains an in-memory
    snapshot cache; the read path must share it, reloading only when the
    revision actually moved, while preserving the deep-copy isolation the
    old read path guaranteed to callers.
    """
    repo = SQLiteWorldRepository(tmp_path / "world.db")
    calls = 0
    real_load = repo._load_snapshot

    def counting_load(connection):
        nonlocal calls
        calls += 1
        return real_load(connection)

    repo._load_snapshot = counting_load

    # __init__ primes the cache, so the first read is already a hit.
    base = repo.snapshot
    assert calls == 0
    assert base.revision == 0

    repo.upsert(
        "observation",
        Observation(id="obs-cache-1", type="note", content={"summary": "s"}),
        actor="agent:test",
    )
    advanced = repo.snapshot
    assert advanced.revision == base.revision + 1
    # The append path maintains the cache itself, so the read after the
    # append is still a hit: no reload happened.
    assert calls == 0
    assert any(obs.id == "obs-cache-1" for obs in advanced.observations.values())

    first = repo.snapshot
    second = repo.snapshot
    assert calls == 0  # unchanged revision: served from cache, no reload
    assert first == second
    assert first is not second  # deep-copy isolation preserved

    # Mutating a returned snapshot must not corrupt the cache.
    first.revision = 99999
    assert repo.snapshot.revision == base.revision + 1
    assert calls == 0

    # A writer outside this instance (a resumed run, a backup restore)
    # moves the revision behind the cache's back; the next read must
    # detect the moved revision and reload exactly once.
    other = SQLiteWorldRepository(tmp_path / "world.db")
    other.upsert(
        "observation",
        Observation(id="obs-external", type="note", content={"summary": "x"}),
        actor="agent:test",
    )
    refreshed = repo.snapshot
    assert refreshed.revision == base.revision + 2
    assert calls == 1  # the stale cache was invalidated by the revision check
    assert any(obs.id == "obs-external" for obs in refreshed.observations.values())
