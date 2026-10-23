# ADR 0002: Introduce the incremental causal-kernel appender seam

- **Status:** Accepted
- **Date:** 2026-10-23
- **Scope:** Migration Stage 0-1
- **Related designs:**
  [incremental causal-kernel refactor](../superpowers/specs/2026-10-23-causal-kernel-incremental-refactor-design.md),
  [first-principles architecture v3](../superpowers/specs/2026-10-22-first-principles-architecture-design.md)
- **Supersedes:** none
- **Related ADR:** [ADR 0001](0001-causal-ledger.md)

## Context

ADR 0001 established `ledger.db` as an append-only shadow beside the authoritative v2 trace. The
first implementation let `TraceRecorder` construct `LedgerStore` directly. That worked for M1, but
it left the future causal kernel coupled to a concrete SQLite class and offered no transaction seam
for future action-admission and reservation batches.

The incremental design chooses a causal-kernel-first migration. Stage 0-1 freezes that direction
without switching any production read path to the ledger.

## Decision

Add `harness.kernel.LedgerAppender` as the narrow write port used by the trace recorder:

```text
TraceRecorder -> LedgerAppender -> SQLiteLedgerAppender -> LedgerStore -> ledger.db
```

`SQLiteLedgerAppender` is the only concrete implementation in this stage. `TraceRecorder` accepts an
injected `LedgerAppender` for failure and seam testing; otherwise it constructs the SQLite adapter
under the existing `HARNESS_LEDGER_SHADOW` policy.

`LedgerStore` also gains `append_batch(events)`, which normalizes all trace events before opening one
`BEGIN IMMEDIATE` transaction. A conflict or write failure rolls back the whole batch. This is the
transaction seam later stages will use for coupled causal records such as action admission and
budget reservation.

## Invariants

1. One Run has one writer path into one ledger.
2. The v2 trace line remains written before the shadow append.
3. Store schema `harness/ledger-store/v1` and event schema `harness/ledger-event/v1` are unchanged.
4. Event IDs remain unique; identical re-appends return the existing record and conflicting content
   raises `LedgerConflict`.
5. Sequence numbers and the SHA-256 hash chain continue across reopen and batch boundaries.
6. A shadow failure never changes the Run status or `result.json`; the first failure writes
   `ledger.error.json` and disables further shadow appends for that recorder.
7. Trace data is sanitized before it reaches the ledger; no credential-bearing payload is added by
   this seam.

## Non-goals

- No World, Context, Progress, Result or Checkpoint read path consumes the ledger in this stage.
- No M2 projection switch is started.
- No action admission, effect classification, budget reservation or unknown reconciliation is
  claimed by this ADR.
- No exactly-once guarantee, cryptographic tamper proof or malicious-host defense is added.
- No benchmark-specific behavior enters the kernel.

## Rollback

Set `HARNESS_LEDGER_SHADOW=0` to disable automatic shadow construction, or inject a disabled
`LedgerAppender` in tests and future composition roots. Existing ledger artifacts remain
non-authoritative and require no schema downgrade or data migration.

## Consequences

- The control spine deliberately gains `harness.kernel` and `harness.kernel.appender`.
- The package ceiling is re-measured at 60 modules and 16,972 source lines.
- Future stages can introduce transaction batches and alternative appenders without changing
  `TraceRecorder` or the v2 trace contract.
- The system still has two write-side representations during the migration window; projection
  comparison remains necessary until the later deletion gates pass.

## Evidence

- `tests/test_kernel_appender.py`: single/batch delegation and conflict semantics.
- `tests/test_trace_appender.py`: injected appender use and fail-closed shadow disablement.
- `tests/test_ledger.py`: atomic batch rollback, chain continuity and golden projection.
- `tests/test_architecture.py`: frozen control-spine registration and concrete-store import guard.
- `tests/test_control_metrics.py`: measured module and source-line ceilings.
