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

**Non-goals:** distributed workflow orchestrator, required DAG, reimplementation of Pi's internal reasoning loop.

