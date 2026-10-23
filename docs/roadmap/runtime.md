# RUN — Runtime, solver and recovery

**Scope:** `solver_loop.py`, `solver_policy.py`, `runtime/`, `progress.py`, `session.py`, `orchestrator.py`, `benchmark/runner.py`.

### RUN-01 [x] P0 — Explicit run state machine
Deliver: allowed transitions among created/running/paused/recovering/verifying/succeeded/failed/exhausted/cancelled, with pure decision separated from event normalization and disk side effects, and one lifecycle policy for local and benchmark paths.
Accept: table-driven transition tests, illegal-transition rejection, exactly one terminal decision, cancellation-vs-success races, no benchmark import in runtime.
State: `runtime.lifecycle.RunLifecycle` is wired into both run paths and rejects illegal or second terminal transitions.

### RUN-02 [~] P0 — Crash-safe checkpoints and action reconciliation
Deliver: durable agent cursor, World revision, budget, feedback cursor and in-flight action IDs plus a recovery protocol for the dispatch/effect/feedback/checkpoint crash windows. This is not an exactly-once resume guarantee.
Accept: fault-injection tests at every boundary; unknown effects never auto-retried; mismatched or corrupt checkpoints detected; no double billing or duplicate events.
State: per-event checkpoints (`harness/checkpoint/v2` with atomic fsync of file and directory), `--resume-run` for Pi RPC, durable progress ledger, optimistic revision checks and operator reconciliation under a per-run cross-process lock are implemented. Outstanding: the Docker-backed recovery fault matrix.

M1 causal shadow ledger (2026-10-22). Added `harness.ledger` as an append-only SQLite WAL store
beside each Run, with monotonic sequence numbers, a SHA-256 hash chain, trust-tier provenance and a
best-effort replay comparison after terminal persistence. Ledger failures are isolated to
`ledger.error.json` and cannot alter the Run status or `result.json`. v2 trace, World, Context,
Result, Progress and Checkpoint reads remain authoritative; M2 is not started. Evidence:
`tests/test_ledger.py`, `tests/test_smoke.py`, and the frozen contract in
[ADR 0001](../adr/0001-causal-ledger.md); targeted `pytest` run passed 31 tests and the full suite
passed 513 tests with one Starlette/httpx deprecation warning. The Docker-backed recovery fault
matrix and a live TSec sweep remain unexecuted.

Stage 0-1 of the incremental causal-kernel refactor is implemented. `TraceRecorder` now writes
shadow events through the `harness.kernel.LedgerAppender` port; `SQLiteLedgerAppender` delegates to
the append-only store, and `LedgerStore.append_batch` commits or rolls back a batch atomically. The
v2 trace remains the production read path and M2 is not started. Evidence:
`tests/test_ledger.py`, `tests/test_ledger_baseline.py`, `tests/test_kernel_appender.py`,
`tests/test_trace_appender.py`, `tests/test_architecture.py`, `tests/test_control_metrics.py`; the
frozen decision is [ADR 0002](../adr/0002-incremental-causal-kernel.md).

### RUN-03 [x] P1 — Event processing and bounded autonomy
Deliver: persisted event identity for dedupe, stable ordering and world-event revisions, plus a pure external stop decision that never infers replanning or exhaustion from Agent behavior.
Accept: duplicate/reordered event tests, goal verification dominates generic completion, deterministic counterexamples show diagnostic counters cannot stop the loop, reproducible trace transitions.
State: `solver_policy.decide_solver_action` is the pure decision point. It stops only when the remaining external budget reaches zero; the progress counters remain diagnostic evidence.

### RUN-04 [~] P2 — Resource ownership
Deliver: one owner for container, session, gateway and task teardown with consistent timeouts, cancellation and idempotent terminal persistence.
Accept: forced kill, signal interruption and external benchmark teardown tests; no leaked resources or overwritten terminal results.
State: atomic first-writer-wins `result.json`, SIGINT/SIGTERM cancellation, single idempotent benchmark teardown bounded to 30s and per-run action-owner locking are implemented. Outstanding: Docker and live external benchmark teardown evidence.

### RUN-05 [~] P1 — Terminal closure for externally evaluated runs
Deliver: a scored, wall-clock-bounded run closes by handing in its best candidate and its report instead of only a report, and the loop stops spending turns on feedback it cannot act on.
Accept: the finalize steer asks for a candidate submission when the adapter is score-based; a run that reached half its wall clock with zero submissions is steered once; a run that already submitted is not; world revisions never become Agent steers; pending and unavailable verdicts are trace-only; run metrics distinguish "never tried" from "tried and rejected".
State: implemented. `SolverLoop` takes `finalize_requests_submission` and `submission_due_fraction`; `BenchmarkRunner` enables both for benchmark runs and records `submissions_attempted` / `first_submission_ms`; `ProgressLedger` counts `submissions_attempted`. Evidence: [2026-10-16 sweep review](../tsec-run-review-2026-10-16.md), coverage in `tests/test_solver_loop.py`, `tests/test_benchmark_streaming.py`. The 15 losing TSec runs on 2026-10-16 all ended with zero submissions, which is the failure shape this item targets. Outstanding: a live sweep to measure time-to-first-submission and the steer-count drop on a real run.

### RUN-06 [~] P0 — In-run pacing and a visible clock
Deliver: the Agent can see its remaining wall clock and budget without asking (rendered into the
Agent-facing context header and readable through the workspace projection), and the benchmark
path keeps RUN-05's four closure switches on by default while the local task path keeps them off.
Accept: offline tests cover the rendered budget block and the per-path defaults; replaying a
historical trace does not change the submission path of a run that already succeeded.
State: implemented. `ProgressLedger.agent_budget_lines` renders a deterministic remaining-window
line, `WorldContextBuilder.render` carries it inside the focus block (a run with no budget lines
renders byte-identically to before), and `SolverLoop` rewrites the projection on every event so
the clock actually ticks. A tool result over `SLOW_TOOL_CALL_MS` (180s) is recorded as `tool.slow`
telemetry without interrupting the Agent, and the opt-in Pi extension clamps the requested bash
timeout to 180s in its `tool_call` hook. The benchmark path enables submission closure; the local
task path leaves it off. Evidence:
`tests/test_solver_loop.py`, `tests/test_world_context.py`, `tests/test_benchmark_streaming.py`,
`tests/test_pi_adapter.py`. The competition configs (`agents/examples/pi-tsec-opencode.yaml`,
`agents/examples/zc-offensive.yaml`) now opt into `pi.harness_extension`, which clamps one bash
call to 180s and spills oversized tool output; every other config keeps the default, pinned by
`tests/test_models.py`. Outstanding: the live pacing effect needs the BEN-07 re-run.
Evidence: 19 tool calls over 180s (longest 927s, 51% of the window) and 39 budget steers that only
arrived after the budget was nearly gone; `world.context.txt` carried no budget section at all.

### RUN-07 [~] P1 — Remove the dead feedback edges
Deliver: the Agent-facing context is a bounded fact summary instead of a dump of raw
`tool.execution` and `action:*` records with full provenance; Agent-authored world changes are
not echoed back as steers in benchmark mode and `pending` verdicts only reach the trace.
Accept: replaying the historical traces drops per-run steers from the hundreds to single digits
(a-05 received 99 world updates plus 39 pending verdicts while failing to submit); the projection
stays inside the WLD-05 token budget.
State: code complete; the live A/B remains. World revisions and pending/unavailable verdicts are
now trace/audit facts only, never Agent observations. The projection is a fact summary:
`world/context.py` sources semantic observations
and the action tally from the snapshot, so Harness bookkeeping (`tool.execution`, one record per
command) can no longer fill the retrieval limit and hide the findings; bookkeeping is summarised
as a count with a failed tally, actions render as a tally plus a bounded eight-line tail, and the
Agent-facing lines carry the trust tier and timings instead of the full provenance blob
(actor/event ids). Measured on the 40 stored `world.db` snapshots from the sweep: the projection
drops from a median 17,922 characters (always hitting its budget) to 4,567, with every semantic
observation preserved. Evidence: `tests/test_world_context.py`, `tests/test_solver_loop.py`.
Outstanding: the steer-count effect needs the BEN-07 re-run. Evidence: 2,261 of 3,154 agent
feedback records were zero-information `world.state.updated`; 656 of 656 action verdicts were
`pending`.

### RUN-09 [x] P0 — Agent-native thin control law
Deliver: the Harness stop law consumes only external budget state; repeated commands, no-progress
counters, slow tools, budget thresholds and automatic platform hints cannot stop or steer the
Agent. Agent-facing steering is limited to operator hints, closure/submission instructions and
real verdicts.
Accept: property tests show diagnostic counters cannot change the stop decision; replay tests show
repetition, budget-threshold, slow-tool, world-revision and pending-verdict paths deliver no
observations; removed CLI flags are absent from docs; targeted and full suites pass.
State: implemented. `solver_policy.decide_solver_action` accepts only
`remaining_budget_fraction`; the automatic platform-hint and `--max-unconfirmed-progress` surfaces
are removed; steer metrics are limited to `hint`, `finalize`, `submission` and `verification`.
Evidence: `tests/test_control_law_properties.py`, `tests/test_control_properties.py`,
`tests/test_solver_loop.py`, `tests/test_benchmark_streaming.py`, `tests/test_docs_consistency.py`.

**Non-goals:** distributed workflow orchestrator, required DAG, reimplementation of Pi's internal reasoning loop.

