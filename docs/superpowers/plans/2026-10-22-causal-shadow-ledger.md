# Causal Shadow Ledger Implementation Plan

> **For execution:** use `superpowers:executing-plans` with TDD. This plan implements migration
> stages M0 and M1 from
> `docs/superpowers/specs/2026-10-22-first-principles-architecture-design.md`. It does not switch
> any production read path to the ledger. v2 remains authoritative.

**Goal:** add one append-only SQLite causal ledger beside every run, mirror existing trace events
into it through a feature-flagged shadow path, and prove that replay reconstructs the v2 terminal,
objective, action, budget and submission projections without changing run outcomes.

**Architecture:** `src/harness/ledger.py` owns the append-only record, hash chain, SQLite store,
trace-to-record adapter, replay projections and v2 comparison. `TraceRecorder` is the single shadow
append point. `persist_run_result` asks the recorder for a best-effort comparison after the terminal
trace event. A ledger failure is recorded as a warning artifact and never changes the run result.

**Tech stack:** Python 3.11+, stdlib `sqlite3`/`hashlib`/`json`, pytest, Ruff.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/harness/ledger.py` | immutable records, SQLite WAL store, hash chain, trace import, projections, comparison |
| `src/harness/trace.py` | optional shadow append after the v2 trace line; failure isolation; comparison hook |
| `src/harness/runtime/results.py` | trigger best-effort ledger comparison after `run.finished` |
| `src/harness/solver_loop.py` | include tool identity in tool trace payload so replay can reconstruct actions |
| `tests/test_ledger.py` | unit/integration tests for the ledger and projection contract |
| `tests/test_smoke.py` | local end-to-end proof that a real run writes and verifies its shadow ledger |
| `docs/adr/0001-causal-ledger.md` | M0 frozen assumptions, schema, flag and acceptance gates |
| `docs/architecture.md` | point to the ADR and current M1 status |
| `docs/roadmap/runtime.md`, `docs/roadmap/quality.md` | record M1 evidence and measured ceilings |
| `tests/test_architecture.py`, `tests/test_control_metrics.py` | deliberate control-spine and size budget update |

## Task 1: Freeze the ledger contract and prove failure isolation

**Files:**
- Create: `tests/test_ledger.py`
- Modify: `src/harness/ledger.py`
- Modify: `src/harness/trace.py`

**Interfaces:**
- `LedgerStore(path: Path)` opens or creates `harness/ledger-store/v1`.
- `LedgerStore.append_trace_event(event: Mapping[str, Any]) -> LedgerRecord`.
- `LedgerStore.records() -> list[LedgerRecord]`.
- `LedgerStore.verify() -> None` raises `LedgerCorruption` on a broken sequence/hash chain.
- `ledger_db_path(trace_path: Path) -> Path` returns `trace_path.parent / "ledger.db"`.
- `shadow_ledger_enabled(value: str | None = None) -> bool` uses `HARNESS_LEDGER_SHADOW`; unset means
  enabled, `0/false/no/off` means disabled.
- `TraceRecorder(..., shadow_ledger: bool | None = None, ledger_path: Path | None = None)`.
- `TraceRecorder.ledger_error: str | None`; first shadow failure is isolated and written once to
  `ledger.error.json`.
- Ledger record fields are exactly:
  `ledger_seq, event_id, run_id, attempt_id, producer, trust_tier, kind, schema_version,
  occurred_at, causation_id, correlation_id, payload, artifact_refs, prev_hash, record_hash,
  content_hash`.
- Trust tiers are `authorized`, `external` and `untrusted`; `agent*` producers are untrusted,
  `gateway` is external, and Harness/verifier producers are authorized.

- [ ] **Step 1: Write failing tests**

Add tests that assert: two emitted trace events produce one ledger row per trace line, shared
`event_id`, monotonic `ledger_seq`, a valid hash chain, and `artifact_refs` extracted from a nested
objective evidence object. Assert direct `UPDATE ledger_records` and `DELETE FROM ledger_records`
both raise `sqlite3.IntegrityError`. Assert a duplicate `event_id` with different payload raises
`LedgerConflict`.

Add a failure-isolation test that points `ledger_path` at a directory, emits one event, and asserts
the normal trace line still exists, `ledger_error` is non-empty, and `ledger.error.json` is a regular
file. Add a flag test that `HARNESS_LEDGER_SHADOW=0` suppresses `ledger.db`.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`
Expected: collection/import failures because `harness.ledger` and the new constructor options do not
exist.

- [ ] **Step 3: Implement the ledger store and trace bridge**

Create `ledger.py` with the frozen constants `LEDGER_STORE_SCHEMA_VERSION =
"harness/ledger-store/v1"`, `LEDGER_EVENT_SCHEMA_VERSION = "harness/ledger-event/v1"`,
`GENESIS_HASH = "0" * 64` and `SHADOW_LEDGER_ENV = "HARNESS_LEDGER_SHADOW"`.

Use a persistent SQLite connection with WAL, `synchronous=FULL`, `busy_timeout=5000`, a process
lock and `BEGIN IMMEDIATE`. Store canonical JSON in `payload_json` and `artifact_refs_json`. Create
`BEFORE UPDATE` and `BEFORE DELETE` triggers that call `RAISE(ABORT, 'ledger is append-only')`.
Compute `content_hash` over the immutable event content and `record_hash` over the full record
including `prev_hash`; `verify()` checks contiguous sequence numbers and both hash rules.

In `TraceRecorder.emit`, write the existing trace line first, then append the shadow record. Catch
every ledger exception, disable further shadow attempts for that recorder, and write a redacted
single `ledger.error.json` warning. Never re-raise a ledger error.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`
Expected: all ledger-store and failure-isolation tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/harness/ledger.py src/harness/trace.py tests/test_ledger.py
git commit -m "feat(ledger): add append-only shadow ledger"
```

## Task 2: Replay trace into deterministic run projections

**Files:**
- Modify: `tests/test_ledger.py`
- Modify: `src/harness/ledger.py`

**Interfaces:**
- `import_trace(trace_path: Path, ledger_path: Path) -> list[LedgerRecord]` imports JSONL traces.
- `project_records(records: Sequence[LedgerRecord]) -> LedgerProjection`.
- `compare_ledger_to_v2(records, result: Mapping[str, Any], progress: Mapping[str, Any]) ->
  LedgerComparison`.
- `write_ledger_comparison(ledger_path: Path, run_dir: Path) -> dict[str, Any]` writes
  `ledger.comparison.json`.
- `LedgerProjection` exposes `run_id`, `terminal`, `objective_status`, `last_action`,
  `active_actions`, `budget`, `submissions`, `world_revision` and `event_count`.
- Budget keys are `tokens`, `model_calls`, `tool_calls`, `cost_usd`, matching `progress.json`.
- Submissions keys are `proposed`, `accepted`, `rejected`.

- [ ] **Step 1: Write failing replay/comparison tests**

Import `tests/fixtures/run_metrics/trace_excerpt.jsonl` through `import_trace`. Assert the projection
has two action IDs (`call-slow`, `call-fast`), `tool_calls == 0` because the excerpt has no
`tool.call`, `tokens == 37820`, and no terminal. Assert `verify()` passes after import.

Build a complete small v2 directory with `result.json` and `progress.json`: terminal `finished`,
objective verdict `verified`, one action, two model calls, one tool call, 120 tokens and two
attempted submissions. Mirror the matching trace events. Assert comparison `ok is True` and that
`write_ledger_comparison` produces the same projection deterministically when called twice.

Mutate the persisted result status and assert the comparison reports a `terminal.status` mismatch
rather than raising.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`
Expected: new replay/comparison tests fail because the projection functions do not exist.

- [ ] **Step 3: Implement deterministic replay and comparison**

Replay trace events in `ledger_seq` order. Derive terminal state from the last `run.finished`,
objective status from the last `objective.verdict`, action state from `tool.call`/`tool.result`,
budget from `model.request`/`model.usage`/`tool.call`/`tool.denied`, submissions from
`benchmark.submission.proposed`/`benchmark.submission.result`, and world revision from the largest
valid revision-like field. Ignore malformed usage values exactly as `charge_usage_event` does.

Compare terminal `status/success/score`, objective status, last action identity/status, active action
IDs/status, budget counters and submitted-attempt count. Use a `1e-9` float tolerance. Return
structured mismatches with `field`, `projected` and `v2` values; never raise for a normal mismatch.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_ledger.py -q`
Expected: all replay and comparison tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/harness/ledger.py tests/test_ledger.py
git commit -m "feat(ledger): replay causal projections"
```

## Task 3: Wire shadow comparison into real runs without changing outcomes

**Files:**
- Modify: `src/harness/solver_loop.py`
- Modify: `src/harness/runtime/results.py`
- Modify: `src/harness/trace.py`
- Modify: `tests/test_ledger.py`
- Modify: `tests/test_smoke.py`

**Interfaces:**
- `TraceRecorder.finalize_shadow(run_dir: Path) -> dict[str, Any] | None` runs verification and
  comparison best-effort. A failure sets `ledger_error` and writes the warning; it never raises.
- `persist_run_result` calls `trace.finalize_shadow(run_dir)` after emitting `run.finished`.
- Tool trace payloads include `tool` and `skill_id` in addition to `tool_call_id`,
  `source_event_id`, `is_error` and `execution_status`.

- [ ] **Step 1: Write failing integration tests**

Add a unit test that calls `persist_run_result` with a complete result/progress pair and asserts
`ledger.comparison.json["ok"] is True`. Add a smoke assertion that the local proof run creates
`ledger.db`, the comparison is okay, and the original result/status is unchanged. Add a test that a
comparison mismatch is visible in `ledger.comparison.json` while `persist_run_result` still writes
the same `result.json`.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_ledger.py tests/test_smoke.py -q`
Expected: comparison files are absent and integration tests fail.

- [ ] **Step 3: Implement terminal comparison and action trace enrichment**

Call `finalize_shadow` after the terminal trace event. Emit tool name and skill identity in the
existing tool trace payload. Keep all comparison work best-effort and after the atomic v2 result
write.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `.venv/bin/python -m pytest tests/test_ledger.py tests/test_smoke.py -q`
Expected: all ledger, comparison and smoke tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/harness/solver_loop.py src/harness/runtime/results.py src/harness/trace.py \
  tests/test_ledger.py tests/test_smoke.py
git commit -m "feat(runtime): verify shadow ledgers at run completion"
```

## Task 4: Freeze M0, update the control spine and record evidence

**Files:**
- Create: `docs/adr/0001-causal-ledger.md`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap/runtime.md`
- Modify: `docs/roadmap/quality.md`
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_control_metrics.py`

**Interfaces:**
- ADR schema version: `harness/ledger-store/v1`.
- ADR event schema version: `harness/ledger-event/v1`.
- Shadow flag: `HARNESS_LEDGER_SHADOW`; enabled by default in M1, disabled with `0`.
- ADR records that v2 remains authoritative and M2 is not started.

- [ ] **Step 1: Write failing architecture/doc tests**

Add `harness.ledger` to the deliberate `CONTROL_SPINE` set and update the module ceiling only after
measuring the new tree. Add a docs test assertion or a short roadmap state check that the ADR names
the flag, hash-chain limitation and rollback rule. Run the architecture and docs checks before the
ADR exists and confirm the missing-path/state assertion fails.

- [ ] **Step 2: Run the checks and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_architecture.py tests/test_docs_consistency.py \
  tests/test_control_metrics.py -q`
Expected: the deliberate architecture/doc update is missing, so at least one test fails.

- [ ] **Step 3: Write the ADR and measured evidence**

Document the immutable assumptions, record/table schema, trust tiers, event mapping, hash-chain
limitation, default-on flag, failure isolation, replay comparison and rollback. Update
`docs/architecture.md` to say M1 is implemented in shadow mode and v2 remains authoritative.
Update RUN-02/QA-04 with the commands and local proof evidence. Re-measure the actual module count and
source lines, then set the ceilings to the measured values rather than guessing.

- [ ] **Step 4: Run the complete verification matrix**

Run, in order:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest tests/test_ledger.py tests/test_architecture.py \
  tests/test_docs_consistency.py tests/test_control_metrics.py tests/test_smoke.py -q
.venv/bin/python -m pytest -q
```

Expected: Ruff clean; targeted tests pass; full suite passes. Record any environment-dependent skips.

- [ ] **Step 5: Commit and deliver**

```bash
git add docs/adr/0001-causal-ledger.md docs/architecture.md docs/roadmap/runtime.md \
  docs/roadmap/quality.md tests/test_architecture.py tests/test_control_metrics.py
git commit -m "docs(architecture): freeze the causal ledger migration"
git push -u origin feat/causal-shadow-ledger
```

Then merge the verified branch into the latest `main` without force-push and report the feature and
`main` SHAs. Do not start M2 projection sourcing in this change.

## Review Focus

- **Crash/failure isolation:** a ledger failure, duplicate event ID, corrupted chain or unwritable
  database must never change `result.json` or the run status.
- **Truth separation:** `claim`-like trace events must not project to objective verified; only a
  typed `objective.verdict` can set `objective_status`.
- **Accounting parity:** malformed/negative usage telemetry must be ignored exactly as
  `charge_usage_event` ignores it.
- **Recovery:** reusing an existing ledger must continue the hash chain; resume must not duplicate a
  trace event or silently reset sequence numbers.
- **Scope:** no production read path reads the ledger yet; M2 stays blocked until this shadow
  comparison evidence exists.
