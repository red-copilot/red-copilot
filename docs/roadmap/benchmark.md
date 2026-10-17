# BEN — Benchmark, evaluation and offline readiness

**Scope:** `benchmark/`, `benchmarks/`, `docs/tsec.md`. The generic adapter, the TSec adapter and the frozen fixtures exist; the suite, scoring and evaluation tooling was removed as open-loop complexity during the control-spine simplification.

### BEN-01 [x] P1 — Common benchmark lifecycle
Deliver: stable discover/provision/submit/evaluate/teardown contracts with result ownership and idempotent cleanup, and no SDK code in domain or runtime layers.
Accept: contract tests for unavailable service, failed provision, submission retry, timeout, teardown after crash and unexpected API responses.
State: implemented; the final evaluator is the authority, teardown runs in `finally` under a single idempotent owner bounded to 30s, and coverage lives in `tests/test_benchmark_streaming.py` and `tests/test_tsec_adapter.py`.

### BEN-02 [x] P1 — Offline readiness gate
Deliver: preflight checks for images, tools, model access, task inputs and SDK integration without mandatory public Internet, plus clear egress policy and actionable diagnostics.
Accept: exercise an authorized target-only environment with public egress disabled and produce actionable missing-dependency errors before a run.
State: `harness preflight` (with `--tsec`), topology, credential and network-profile checks plus bounded task-tree validation are implemented and never pull images or contact services. Target-only reachability and public-egress denial run in CI (`tests/test_network_profiles_docker.py`) after the probe image build, and passed locally against `harness-isolation-probe:ci`.

### BEN-03 [!] P1 — Cross-domain evaluation (descoped 2026-10-13)
Per-domain pass@k reporting lived in the removed suite tooling. Nine synthetic offline fixtures across general, coding, file, Web, Pwn, Reverse, Crypto, Forensics and Misc remain as regression fixtures with independent verifiers and fixed seeds (`tests/test_benchmark_fixtures.py`); they are smoke contracts, not performance evidence, and a periodic report over real benchmark tasks is not an acceptance path.

### BEN-04 [!] P2 — Reproducibility (descoped 2026-10-13)
Manifest and artifact-hash reporting lived in the removed evaluation tooling. Output sanitization for benchmark adapters, reduction of run-result exceptions to a class plus a details-omitted marker, and TSec flag extraction that no longer harvests prompt placeholders remain in the runtime and are covered by `tests/test_tsec_adapter.py`.

### BEN-05 [~] P1 — Sweep orchestration reliability
Deliver: a TSec sweep that fails fast and cleanly on a platform-wide outage, keeps one bad case from aborting the rest, never leaves an empty run directory behind, resumes past locally solved cases, and writes one summary artifact per run.
Accept: platform outage exits 3 inside a bounded window with zero run directories; a case-local start failure records itself and the remaining cases still run; a platform-level failure stops new dispatches while in-flight cases keep their artifacts; locally solved cases are skipped; the summary carries per-case status and tallies; `harness gc-runs` reports artifact-free directories.
State: implemented. `TSecPlatformError` classifies `vpn_unreachable` / `platform_unreachable` / `instance_conflict`, `run_tsec_challenges` runs a bounded preflight (`check_vpn` + `list_challenges`, 45s) before selecting cases, dispatches through a worker queue that stops on a platform-level failure, and writes `sweep-summary-<UTC>.json`; `BenchmarkRunner.run_case` provisions before creating the run directory; `--preflight/--no-preflight`, `--resume/--rerun`, `--summary-out` and exit codes 0/3/4 exist; `harness gc-runs` cleans artifact-free directories (dry run by default). Evidence: [2026-10-16 sweep review](../tsec-run-review-2026-10-16.md), coverage in `tests/test_tsec_adapter.py`, `tests/test_benchmark_streaming.py`. Measured on the live platform while the VPN health check was failing: exit 3 after 5.0s with 0 run directories, versus 28 blind retries leaving 43 empty directories in the same state on the same day. Outstanding: the acceptance re-run of the 10 unsolved/never-run challenges once the platform is reachable.

### BEN-06 [~] P1 — Run and sweep metric coverage
Deliver: one metric vocabulary shared by `result.json`, `sweep-summary-<UTC>.json` and
`harness report` covering submission timing (`submissions_attempted`, `first_submission_ms`,
`submitted_at_fraction`), steer volume by kind, tool time and the slowest calls, and the final
context size, so "never tried" is distinguishable from "tried and rejected" without hand-written
`jq`.
Accept: every field is reproducible offline from a checked-in trace fixture; a run that never
submitted reports `submissions_attempted=0` with a null first-submission time.
State: implemented. `progress.py` owns the steer vocabulary and `runtime/results.py` the
offline replay (`steer_counts`, `derive_run_metrics`, `submission_fraction`); `SolverLoop` counts
only the observations it actually delivers (so a quieted world echo is absent from both the
counter and `agent.feedback.jsonl`), records tool time with a bounded top-five slow list and the
final prompt size, and `BenchmarkRunner` folds the per-session counters into `result.json:metrics`
together with `submitted_at_fraction`. The TSec sweep summary carries the same fields per case and
`harness report` prints them as a loop-health block. Evidence: `tests/test_run_metrics.py`
(checked-in trace and feedback fixtures under `tests/fixtures/run_metrics/`), the counter and
counter-vs-delivery tests in `tests/test_solver_loop.py`, the run-level assertions in
`tests/test_benchmark_streaming.py`, and the sweep-summary tests in `tests/test_tsec_adapter.py`.
Outstanding: the same fields on a live sweep. Evidence base: the 2026-10-16 analysis in
[tsec-improvement-plan-2026-10-16.md](../tsec-improvement-plan-2026-10-16.md) §1.

### BEN-07 [ ] P1 — Post-recovery re-run acceptance
Deliver: the acceptance re-run of the ten unsolved or never-run challenges (`b-03`,
`c-02`..`c-06`, `a-03`, `a-05`, `e2-04`, `e3-04`) plus four solved controls once the platform is
reachable, compared on submission rate, time to first submission, wall clock, steer counts and
tokens.
Accept: each landed item either improves its metric or is rolled back; RUN-07 (steer quieting)
and VER-05 (`unavailable` semantics) are checked first because they can hide real feedback.
State: pending platform availability; the VPN health check still fails as of 2026-10-16.

### BEN-08 [ ] P1 — Hosted-run compliance
Deliver: whatever the platform runs has to satisfy rules that the local sweep never exercised, so
each one is stated as an acceptance line before any packaging work: the container must **start
solving on launch** (the platform injects `BENCHMARK_BASE_URL` and `BENCHMARK_TOKEN` and does not
drive a solve loop from outside); model traffic must go through the platform gateway, whose rule is
"append `.tsecbench.gw` to the provider domain and use `http://`", against a fixed 22-provider
allowlist; the uploaded image is capped at 3 GB; the whole submission gets 8 vCPU and 16 GB; and
nothing task-specific may be baked in - no previous flags, answers or per-challenge notes.
State: pending, and deliberately unspecified: this records the constraints, not the design. The
known gaps, measured against the tree on 2026-10-17: the competition configs point at
`opencode-go` and `openai`, neither of which is on the allowlist and neither of which has an
env-driven endpoint (only `env_passthrough` for the key); `harness run`/`harness tsec` drive the
container from outside, so "starts solving on launch" needs an in-image entrypoint that owns the
loop; the Pi container asks for 4 CPU and 8 GB, and `src/spike_swarm` defaults to three concurrent
challenges with three solvers each, which does not fit one 8 vCPU / 16 GB submission; the
`--seed-notes-from` continuity channel must not ship inside an uploaded image. The image side is
already inside the limit: 2,983,477,199 bytes on disk, 766,589,952 bytes as `docker save` output
(763,061,574 gzipped). Decision needed before implementation: what exactly the uploaded artifact
contains (agent only, or agent plus the benchmark client and loop).
Accept: a locally built image starts solving with only `BENCHMARK_BASE_URL`/`BENCHMARK_TOKEN`
injected and no public egress, and the packaging contains no per-challenge state.

**Non-goals:** hiding task-specific heuristics in the universal solver; evaluation that relies on unverified Agent self-reports.

