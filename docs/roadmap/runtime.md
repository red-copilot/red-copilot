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

### RUN-06 [~] P0 — In-run pacing and a visible clock
Deliver: the Agent can see its remaining wall clock and budget without asking (rendered into the
Agent-facing context header and readable through the workspace projection), and the benchmark
path keeps RUN-05's four closure switches on by default while the local task path keeps them off.
Accept: offline tests cover the rendered budget block and the per-path defaults; replaying a
historical trace does not change the submission path of a run that already succeeded.
State: implemented. `ProgressLedger.agent_budget_lines` renders a deterministic remaining-window
line, `WorldContextBuilder.render` carries it inside the focus block (a run with no budget lines
renders byte-identically to before), and `SolverLoop` rewrites the projection on every event so
the clock actually ticks. A tool result over `SLOW_TOOL_CALL_MS` (180s) emits `tool.slow` and
steers the Agent once per run instead of blocking silently, and the opt-in Pi extension now clamps
the requested bash timeout to 180s in its `tool_call` hook. The benchmark path keeps RUN-05's four
closure switches on and the local task path keeps them off, both pinned by tests. Measured: the
ceiling re-measured at 17,075 lines with the module count unchanged. Evidence:
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
State: mostly implemented. The quieting switches were already on for benchmark runs (RUN-06
pins them). The projection is now a fact summary: `world/context.py` sources semantic observations
and the action tally from the snapshot, so Harness bookkeeping (`tool.execution`, one record per
command) can no longer fill the retrieval limit and hide the findings; bookkeeping is summarised
as a count with a failed tally, actions render as a tally plus a bounded eight-line tail, and the
Agent-facing lines carry the trust tier and timings instead of the full provenance blob
(actor/event ids). Measured on the 40 stored `world.db` snapshots from the sweep: the projection
drops from a median 17,922 characters (always hitting its budget) to 4,567, with every semantic
observation preserved. Evidence: `tests/test_world_context.py`. Outstanding: `pending` verdicts are
still emitted as trace events (they are already not steered), and the steer-count effect needs the
BEN-07 re-run. Evidence: 2,261 of 3,154 agent feedback records were zero-information
`world.state.updated`; 656 of 656 action verdicts were `pending`.

**Non-goals:** distributed workflow orchestrator, required DAG, reimplementation of Pi's internal reasoning loop.

### RUN-08 [~] P1 — Zero-submission closure and repetition precision
Deliver: a scored run that ends without submitting gets one bounded chance to hand in
the candidate it already holds. RUN-09 later removed repetition steering entirely.
Accept: a settled benchmark session with zero submissions receives one
`solver.submission_reask`; if the process has already ended, the runner starts at most
one closing session and skips it when less than 60 seconds remain; local task runs and
runs with an existing submission are unchanged. A successful run that issues different
commands in one family receives no repetition steer; three failures in one family or
three identical argument fingerprints trigger one, and trusted progress resets the
counters. `steer_counts.submission` records submission-due, re-ask and closing-session
steers.
State: implemented offline. Evidence: the 2026-10-16 sweep had 15/15 losing runs with
zero submissions (11 ended voluntarily), and 56 repetition steers landed on 17 of 21
successful runs. Coverage: `tests/test_control_replay.py`, `tests/test_control_properties.py`,
`tests/test_solver_loop.py`, `tests/test_executors.py`, `tests/test_benchmark_streaming.py`.
Outstanding: the BEN-07 live same-case A/B run.

### RUN-09 [~] P1 — Agent-Native thin control
Deliver: remove the residual Harness-side cognition and workflow contract. Agent memory is
notes plus shared findings; `world.context.txt` is a fact-only projection; typed Agent events,
the action-intent workflow, the Focus/next-action projection and action-level verification are gone.
Heuristic repetition, slow-tool, budget-threshold and automatic platform-hint steering are gone;
only budget/wall-clock, authoritative evaluation, submission/finalize/closing closure, context
restart and operator hints remain.
Accept: no new run exposes Agent event/inbox/progress-file channels; a boundary-free run with
no typed Agent events starts; repeated failures do not stop a run; only exhausted budget/time
stops it; progress v4 remains readable and upgrades to v5; submission closure, context restart,
operator hint and benchmark feedback still work; active docs do not teach removed contracts.
State: implemented offline. Evidence: `tests/test_pi_adapter.py`,
`tests/test_solver_loop.py`, `tests/test_world_context.py`, `tests/test_control_properties.py`,
`tests/test_control_law_properties.py`, `tests/test_session_telemetry.py`,
`tests/test_run_metrics.py`, `tests/test_benchmark_streaming.py`,
`tests/test_executors.py`, `tests/test_architecture.py`.
Outstanding: the BEN-07 live same-case A/B run; missing platform credentials and VPN keep the
effect measurement `blocked`.

