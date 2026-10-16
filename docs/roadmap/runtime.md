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

### RUN-03 [x] P1 — Event processing and bounded autonomy
Deliver: persisted event identity for dedupe, stable ordering and world-event revisions, explicit continue/verify/replan/stop decisions and bounded replan rate with budget/time thresholds.
Accept: duplicate/reordered event tests, no-progress loops terminate, goal verification dominates generic completion, reproducible trace transitions.
State: `solver_policy.decide_solver_action` is the pure decision point; replans cap at 8, six no-progress outcomes stop the loop, and 5% remaining budget stops when no progress continues.

### RUN-04 [~] P2 — Resource ownership
Deliver: one owner for container, session, gateway and task teardown with consistent timeouts, cancellation and idempotent terminal persistence.
Accept: forced kill, signal interruption and external benchmark teardown tests; no leaked resources or overwritten terminal results.
State: atomic first-writer-wins `result.json`, SIGINT/SIGTERM cancellation, a parent-death process supervisor for host CLI agents, single idempotent benchmark teardown bounded to 30s and per-run action-owner locking are implemented. Outstanding: Docker and live external benchmark teardown evidence.

### RUN-05 [~] P1 — Terminal closure for externally evaluated runs
Deliver: a scored, wall-clock-bounded run closes by handing in its best candidate and its report instead of only a report, and the loop stops spending turns on feedback it cannot act on.
Accept: the finalize steer asks for a candidate submission when the adapter is score-based; a run that reached half its wall clock with zero submissions is steered once; a run that already submitted is not; Agent-authored world revisions are not echoed back as steers in benchmark mode while Harness-authored ones still are; pending verdicts are recorded but not steered; run metrics distinguish "never tried" from "tried and rejected".
State: implemented. `SolverLoop` takes `finalize_requests_submission`, `submission_due_fraction`, `quiet_world_steers` and `verdict_steers_only` (all default off, so task runs are unchanged); `BenchmarkRunner` enables them for benchmark runs and records `submissions_attempted` / `first_submission_ms`; `ProgressLedger` counts `submissions_attempted`. Evidence: [2026-10-16 sweep review](../tsec-run-review-2026-10-16.md), coverage in `tests/test_solver_loop.py`, `tests/test_benchmark_streaming.py`. The 15 losing TSec runs on 2026-10-16 all ended with zero submissions, which is the failure shape this item targets. Outstanding: a live sweep to measure time-to-first-submission and the steer-count drop on a real run.

### RUN-06 [ ] P0 — In-run pacing and a visible clock
Deliver: the Agent can see its remaining wall clock and budget without asking (rendered into the
Agent-facing context header and readable through the workspace projection), and the benchmark
path keeps RUN-05's four closure switches on by default while the local task path keeps them off.
Accept: offline tests cover the rendered budget block and the per-path defaults; replaying a
historical trace does not change the submission path of a run that already succeeded.
State: pending. Evidence: 19 tool calls over 180s (longest 927s, 51% of the window) and 39
budget steers that only arrived after the budget was nearly gone; `world.context.txt` carried no
budget section at all.

### RUN-07 [ ] P1 — Remove the dead feedback edges
Deliver: the Agent-facing context is a bounded fact summary instead of a dump of raw
`tool.execution` and `action:*` records with full provenance; Agent-authored world changes are
not echoed back as steers in benchmark mode and `pending` verdicts only reach the trace.
Accept: replaying the historical traces drops per-run steers from the hundreds to single digits
(a-05 received 99 world updates plus 39 pending verdicts while failing to submit); the projection
stays inside the WLD-05 token budget.
State: pending. Evidence: 2,261 of 3,154 agent feedback records were zero-information
`world.state.updated`; 656 of 656 action verdicts were `pending`.

**Non-goals:** distributed workflow orchestrator, required DAG, reimplementation of Pi's internal reasoning loop.

