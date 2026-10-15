# BEN — Benchmark, evaluation and offline readiness

**Scope:** `benchmark/`, `benchmarks/`, `docs/tsec.md`. The generic adapter, the TSec adapter and the frozen fixtures exist; the suite, scoring and evaluation tooling was removed as open-loop complexity during the control-spine simplification.

### BEN-01 [x] P1 — Common benchmark lifecycle
Deliver: stable discover/provision/submit/evaluate/teardown contracts with result ownership and idempotent cleanup, and no SDK code in domain or runtime layers.
Accept: contract tests for unavailable service, failed provision, submission retry, timeout, teardown after crash and unexpected API responses.
State: implemented; the final evaluator is the authority, teardown runs in `finally` under a single idempotent owner bounded to 30s, and coverage lives in `tests/test_benchmark_streaming.py` and `tests/test_tsec_adapter.py`.

### BEN-02 [~] P1 — Offline readiness gate
Deliver: preflight checks for images, tools, model access, task inputs and SDK integration without mandatory public Internet, plus clear egress policy and actionable diagnostics.
Accept: exercise an authorized target-only environment with public egress disabled and produce actionable missing-dependency errors before a run.
State: `harness preflight` (with `--tsec`), topology, credential and network-profile checks plus bounded task-tree validation are implemented and never pull images or contact services. Outstanding: a Docker-capable CI run proving target-only reachability and public-egress denial.

### BEN-03 [!] P1 — Cross-domain evaluation (descoped 2026-10-13)
Per-domain pass@k reporting lived in the removed suite tooling. Nine synthetic offline fixtures across general, coding, file, Web, Pwn, Reverse, Crypto, Forensics and Misc remain as regression fixtures with independent verifiers and fixed seeds (`tests/test_benchmark_fixtures.py`); they are smoke contracts, not performance evidence, and a periodic report over real benchmark tasks is not an acceptance path.

### BEN-04 [!] P2 — Reproducibility (descoped 2026-10-13)
Manifest and artifact-hash reporting lived in the removed evaluation tooling. Output sanitization for benchmark adapters, reduction of run-result exceptions to a class plus a details-omitted marker, and TSec flag extraction that no longer harvests prompt placeholders remain in the runtime and are covered by `tests/test_tsec_adapter.py`.

**Non-goals:** hiding task-specific heuristics in the universal solver; evaluation that relies on unverified Agent self-reports.

