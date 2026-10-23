# Causal Kernel Stage 0-1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the causal-kernel baseline and introduce a tested `LedgerAppender` seam without changing v2 production behavior.

**Architecture:** Keep `ledger.db` as a shadow of `trace.jsonl`. Extract a narrow kernel appender protocol around the existing `LedgerStore`, add atomic batch append and injected-writer test seams, then route `TraceRecorder` through that port. World, Context, Progress, Checkpoint, and Result read paths remain unchanged.

**Tech Stack:** Python 3.11+, SQLite WAL, pytest, Ruff, existing `harness.ledger` and `harness.trace` modules.

**Spec:** [2026-10-23-causal-kernel-incremental-refactor-design.md](../specs/2026-10-23-causal-kernel-incremental-refactor-design.md)

## Global Constraints

- v2 remains authoritative in this plan; do not switch World, Context, Progress, Checkpoint, or Result reads to the ledger.
- Preserve `harness/ledger-store/v1` and `harness/ledger-event/v1`; do not rename or migrate existing columns.
- `kernel` must not import FastAPI, Starlette, Docker, subprocess, httpx, or the TSec SDK.
- Every new module on the control spine must be added deliberately to `CONTROL_SPINE`.
- Trace and ledger output must remain sanitized; never add credentials or raw secret-bearing payloads.
- Public event IDs, sequence numbers, hash-chain rules, and `result.json`/`trace.jsonl` read shapes must remain compatible.
- Work offline; no Docker, model credentials, or TSec platform access is required for this plan.
- Follow existing style: Python 3.11+, `from __future__ import annotations`, Ruff line length 100.
- Run targeted tests first, then the full suite; do not claim completion from a subset.

## Review Focus

- A batch containing a new event and a conflicting existing event must leave zero new records.
- A failing injected appender must not fail the run, write a trace, or remain active for later events.
- The new appender seam must not create a second writer, reset the sequence, or weaken hash verification.
- Golden projection output must be deterministic for the same imported trace and must not depend on live wall time.
- New kernel modules and their imports must be explicitly registered in the architecture and source ceilings.

### Task 1: Freeze the historical projection baseline and replay measurement

**Files:**
- Create: `tests/fixtures/kernel/trace_excerpt.projection.json`
- Create: `scripts/ledger_baseline.py`
- Modify: `tests/test_ledger.py`
- Test: `tests/test_ledger_baseline.py`

**Interfaces:**
- Consumes: existing `harness.ledger.import_trace(path, ledger_path)` and `project_records(records)`.
- Produces: `scripts/ledger_baseline.py` with CLI arguments `--events` (default `10000`) and optional `--work-dir`; prints one JSON object containing `event_count`, `append_ms`, `verify_ms`, `project_ms`, and `ledger_bytes`.

- [ ] **Step 1: Write the failing golden-projection test**

Add to `tests/test_ledger.py`:

```python
def test_projection_matches_kernel_golden_fixture(tmp_path: Path) -> None:
    import json
    from harness.ledger import import_trace, project_records

    fixture = Path(__file__).resolve().parent / "fixtures" / "run_metrics" / "trace_excerpt.jsonl"
    golden = Path(__file__).resolve().parent / "fixtures" / "kernel" / "trace_excerpt.projection.json"
    records = import_trace(fixture, tmp_path / "ledger.db")

    assert project_records(records).as_dict() == json.loads(golden.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run it and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_ledger.py::test_projection_matches_kernel_golden_fixture -q`

Expected: FAIL because `tests/fixtures/kernel/trace_excerpt.projection.json` does not exist.

- [ ] **Step 3: Create the golden fixture and baseline script**

Create the golden JSON from the actual `project_records(import_trace(...)).as_dict()` result for `tests/fixtures/run_metrics/trace_excerpt.jsonl`.

Create `scripts/ledger_baseline.py` using `argparse`, `tempfile.TemporaryDirectory`, `time.perf_counter()`, `LedgerStore`, and `project_records`. It must append synthetic events with the same field shape as trace events, then verify and project the store. It must emit JSON to stdout and exit non-zero on invalid input.

- [ ] **Step 4: Write the failing script test**

Add `tests/test_ledger_baseline.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_ledger_baseline_reports_phase_timings() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "ledger_baseline.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--events", "50"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["event_count"] == 50
    assert set(payload) >= {
        "events_per_second",
        "append_ms",
        "verify_ms",
        "project_ms",
        "ledger_bytes",
    }
    assert payload["ledger_bytes"] > 0
```

- [ ] **Step 5: Run the new tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_ledger.py::test_projection_matches_kernel_golden_fixture tests/test_ledger_baseline.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/kernel/trace_excerpt.projection.json scripts/ledger_baseline.py tests/test_ledger.py tests/test_ledger_baseline.py
git commit -m "test(kernel): freeze causal projection baseline"
```

### Task 2: Make ledger batch append atomic

**Files:**
- Modify: `src/harness/ledger.py`
- Modify: `tests/test_ledger.py`

**Interfaces:**
- Consumes: existing `LedgerStore.append_trace_event`, `LedgerStore.append`, `LedgerStore.verify`.
- Produces: `LedgerStore.append_batch(self, events: Sequence[Mapping[str, Any]]) -> tuple[LedgerRecord, ...]`.
- Produces: internal `LedgerStore._append_locked(...)` used by both single and batch appends; it must not begin or commit its own transaction.

- [ ] **Step 1: Write the failing atomic-batch tests**

Add to `tests/test_ledger.py`:

```python
def test_append_batch_is_atomic_on_conflict(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path / "ledger.db")
    existing = _event("evt-existing", "tool.call", {"tool": "bash"})
    store.append_trace_event(existing)

    conflict = _event("evt-existing", "tool.result", {"is_error": True})
    fresh = _event("evt-fresh", "tool.result", {"tool_call_id": "call-1"})
    with pytest.raises(LedgerConflict):
        store.append_batch([fresh, conflict])

    assert [record.event_id for record in store.records()] == ["evt-existing"]
    assert [record.ledger_seq for record in store.records()] == [1]


def test_append_batch_returns_ordered_records_and_reopens_chain(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = LedgerStore(path)
    records = store.append_batch(
        [
            _event("evt-1", "run.started", {"task": "ledger"}),
            _event("evt-2", "tool.call", {"tool": "bash", "tool_call_id": "call-1"}),
        ]
    )

    assert [record.ledger_seq for record in records] == [1, 2]
    store.verify()
    store.close()

    reopened = LedgerStore(path)
    third = reopened.append_trace_event(_event("evt-3", "tool.result", {"tool_call_id": "call-1"}))
    assert third.ledger_seq == 3
    reopened.verify()
    reopened.close()
```

- [ ] **Step 2: Run them and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_ledger.py::test_append_batch_is_atomic_on_conflict tests/test_ledger.py::test_append_batch_returns_ordered_records_and_reopens_chain -q`

Expected: FAIL because `append_batch` does not exist.

- [ ] **Step 3: Refactor the write path and implement atomic batches**

Implement the following signatures in `src/harness/ledger.py`:

```python
def append_batch(self, events: Sequence[Mapping[str, Any]]) -> tuple[LedgerRecord, ...]: ...

def _trace_event_kwargs(self, event: Mapping[str, Any]) -> dict[str, Any]: ...

def _append_locked(
    self,
    *,
    event_id: str,
    run_id: str,
    kind: str,
    occurred_at: str,
    producer: str,
    trust_tier: str,
    payload: Mapping[str, Any],
    attempt_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    schema_version: str = LEDGER_EVENT_SCHEMA_VERSION,
    artifact_refs: Sequence[str] = (),
) -> LedgerRecord: ...
```

`append` and `append_batch` must hold one lock and one `BEGIN IMMEDIATE` transaction. Normalize every event before opening the transaction so an invalid event adds no rows. A conflict or exception must roll back the entire batch. Identical existing events may return their existing record; conflicting content must raise `LedgerConflict`.

- [ ] **Step 4: Run the tests and verify the original chain tests still pass**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harness/ledger.py tests/test_ledger.py
git commit -m "feat(ledger): append causal event batches atomically"
```

### Task 3: Add the kernel appender port

**Files:**
- Create: `src/harness/kernel/__init__.py`
- Create: `src/harness/kernel/appender.py`
- Test: `tests/test_kernel_appender.py`

**Interfaces:**
- Consumes: `harness.ledger.LedgerRecord`, `LedgerStore`.
- Produces: `LedgerAppender` protocol with `path: Path`, `append_trace_event(event: Mapping[str, Any]) -> LedgerRecord`, `append_batch(events: Sequence[Mapping[str, Any]]) -> tuple[LedgerRecord, ...]`, `verify() -> None`, and `close() -> None`.
- Produces: `SQLiteLedgerAppender(path: Path)` implementing that protocol by delegating to one `LedgerStore`.
- The package `harness.kernel` exports `LedgerAppender` and `SQLiteLedgerAppender`.

- [ ] **Step 1: Write the failing appender tests**

Create `tests/test_kernel_appender.py`:

```python
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
```

- [ ] **Step 2: Run it and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py -q`

Expected: FAIL because `harness.kernel` does not exist.

- [ ] **Step 3: Implement the protocol and SQLite adapter**

Create the two files. Keep `appender.py` dependency-light: it may import only `pathlib`, `typing`, `collections.abc`, and `harness.ledger`. Do not add FastAPI, Docker, subprocess, httpx, or TSec imports.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harness/kernel tests/test_kernel_appender.py
git commit -m "feat(kernel): add append-only ledger appender port"
```

### Task 4: Route TraceRecorder through the appender seam

**Files:**
- Modify: `src/harness/trace.py`
- Test: `tests/test_trace_appender.py`

**Interfaces:**
- Consumes: `harness.kernel.LedgerAppender` and `SQLiteLedgerAppender`.
- Produces: `TraceRecorder(..., ledger_appender: LedgerAppender | None = None)`; an injected appender takes precedence over automatic SQLite construction.
- Produces: `_append_shadow` behavior that disables the appender after the first failure and preserves the existing `ledger.error.json` artifact.

- [ ] **Step 1: Write the failing TraceRecorder seam tests**

Create `tests/test_trace_appender.py`:

```python
from __future__ import annotations

from pathlib import Path

from harness.ledger import LedgerRecord
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
        return tuple()

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
```

- [ ] **Step 2: Run them and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_trace_appender.py -q`

Expected: FAIL because `TraceRecorder.__init__` does not accept `ledger_appender`.

- [ ] **Step 3: Implement the injection and remove the concrete store dependency**

Modify `src/harness/trace.py`:

- import `LedgerAppender` and `SQLiteLedgerAppender` from `.kernel`;
- change `_ledger` to `LedgerAppender | None`;
- accept `ledger_appender: LedgerAppender | None = None`;
- preserve `ledger_path` as the comparison target;
- when no appender is injected, construct `SQLiteLedgerAppender(self._ledger_path)` under the existing shadow flag;
- keep `_record_ledger_error`, `_append_shadow`, and `finalize_shadow` semantics.

Do not change `emit` event shape or redaction.

- [ ] **Step 4: Run trace and ledger tests**

Run: `.venv/bin/python -m pytest tests/test_trace_appender.py tests/test_ledger.py tests/test_smoke.py -q`

Expected: PASS, including the existing shadow comparison smoke coverage.

- [ ] **Step 5: Commit**

```bash
git add src/harness/trace.py tests/test_trace_appender.py
git commit -m "refactor(trace): route shadow writes through kernel appender"
```

### Task 5: Register the seam in the architecture and record Stage 1 evidence

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_control_metrics.py`
- Modify: `docs/architecture.md`
- Create: `docs/adr/0002-incremental-causal-kernel.md`
- Modify: `docs/roadmap/runtime.md`

**Interfaces:**
- Consumes: the `harness.kernel` package and `TraceRecorder` appender seam.
- Produces: explicit `CONTROL_SPINE` membership for `harness.kernel` and `harness.kernel.appender`.
- Produces: an updated module/line ceiling based on measured source metrics.

- [ ] **Step 1: Write the failing architecture assertions**

Add to `tests/test_architecture.py`:

```python
def test_trace_does_not_import_concrete_ledger_store() -> None:
    imports = _imports(PACKAGE_ROOT / "trace.py", PACKAGE_ROOT)
    assert "harness.ledger.LedgerStore" not in imports
```

Add `harness.kernel` and `harness.kernel.appender` to `CONTROL_SPINE`.

- [ ] **Step 2: Run it and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_architecture.py::test_trace_does_not_import_concrete_ledger_store tests/test_architecture.py::test_control_spine_import_closure_is_frozen -q`

Expected: at least the concrete-store assertion or frozen closure fails before the changes are complete.

- [ ] **Step 3: Measure and update the metric ceilings**

Run a deterministic measurement over `src/harness/**/*.py` and set `MAX_MODULES` and `MAX_SRC_LINES` to the measured values. Do not lower an existing ceiling: this migration deliberately adds the kernel seam.

Expected: the test reports the measured module count and source-line count; record those exact values in `tests/test_control_metrics.py`.

- [ ] **Step 4: Write the ADR and architecture/roadmap notes**

Create `docs/adr/0002-incremental-causal-kernel.md` with:

- status accepted;
- context: v2 remains authoritative, M1 shadow ledger exists;
- decision: add `LedgerAppender` as the only trace-to-ledger write seam;
- invariants: one writer, atomic batches, hash chain, failure isolation;
- non-goals: no World/Context/Result read switch, no M2 start;
- rollback: inject a disabled appender or disable `HARNESS_LEDGER_SHADOW`.

Update `docs/architecture.md` to link the ADR and describe Stage 1 as implemented in shadow mode. Update `docs/roadmap/runtime.md` under RUN-02 with the new evidence and keep RUN-02 `[~]`; do not mark RUN-02 complete.

- [ ] **Step 5: Run architecture, metrics, and docs checks**

Run: `.venv/bin/python -m pytest tests/test_architecture.py tests/test_control_metrics.py tests/test_docs_consistency.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_architecture.py tests/test_control_metrics.py docs/architecture.md docs/adr/0002-incremental-causal-kernel.md docs/roadmap/runtime.md
git commit -m "docs(kernel): freeze the stage-one appender seam"
```

### Task 6: Run the full verification matrix

**Files:**
- No new files.
- Verify all changes from Tasks 1-5.

- [ ] **Step 1: Run the focused kernel and architecture tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_ledger.py \
  tests/test_ledger_baseline.py \
  tests/test_kernel_appender.py \
  tests/test_trace_appender.py \
  tests/test_architecture.py \
  tests/test_control_metrics.py \
  tests/test_docs_consistency.py \
  tests/test_smoke.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the full suite and lint**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
git diff --check
```

Expected: all tests pass, Ruff reports `All checks passed!`, and `git diff --check` is silent.

- [ ] **Step 3: Record the final evidence**

If the plan is executed in the repository, append the exact commands and counts to `docs/roadmap/runtime.md` under RUN-02. Keep the wording explicit that M1 remains shadow and M2 has not started.

- [ ] **Step 4: Commit the final evidence**

```bash
git add docs/roadmap/runtime.md
git commit -m "test(kernel): record stage-one verification evidence"
```

## Self-Review

- **Spec coverage:** Task 1 freezes the historical golden baseline; Task 2 implements atomic batch append; Task 3 adds the kernel appender seam; Task 4 routes the existing shadow writer through it; Task 5 registers architecture and documentation; Task 6 verifies the full suite. Stages 2-6 remain separate plans.
- **Step scan:** Each task has a failing test, implementation step, pass check, and commit. No step asks an implementer to guess a public schema or switch a production read path.
- **Type consistency:** `LedgerAppender`, `SQLiteLedgerAppender`, `append_batch`, `_append_locked`, and `ledger_appender` are defined once and reused consistently.
- **Review Focus mapping:** atomic batch rollback is Task 2; injected appender failure is Task 4; no second writer/chain reset is Tasks 2-3; deterministic golden projection is Task 1; control-spine registration is Task 5.
- **Proportion:** The plan implements only Stage 0-1 and leaves the larger projection migration to later plans.
