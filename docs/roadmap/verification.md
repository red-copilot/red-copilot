# VER — Verification and evidence

**Scope:** `action_verifier.py`, `verifier.py`, `audit.py`, `solver_loop.py`, World provenance. Agent observations stay claims; only structured trusted evidence can complete an objective.

### VER-01 [x] P0 — Typed evidence and verdict contracts
Deliver: immutable `EvidenceRef`/`ActionVerdict`/`ObjectiveVerdict` carrying action/run IDs, producer, capture time, world revision and artifact hash, with command execution, observed effect and objective completion kept distinct; non-Harness producers rejected.
Accept: unit tests for forged producer, missing action ID, stale revision, conflicting evidence and a failed tool with apparent success text; no false verified verdicts.
State: implemented in `verification.py`; the solver accepts typed action verdicts and persists the versioned verdict/evidence shape. Trust-boundary change already reviewed.

### VER-02 [x] P0 — Trusted verification boundary
Deliver: `VerifierRegistry` authority for `tool_adapter`, the isolated task verifier and the benchmark evaluator, enforced at the integration boundary rather than by a caller-supplied dictionary.
Accept: an integration test with malicious Agent claims and successful command exits failing goal verification; terminal verified goals tie to durable evidence IDs.
State: implemented; Python task verifiers run under bubblewrap with namespaces, no capabilities, read-only inputs and resource limits, failing closed when unavailable. `SolverLoop` backfills a missing Agent expectation from the published plan so action verification never goes dark, and no `verified` verdict can originate from that fallback.

### VER-03 [x] P1 — Durable evidence lifecycle and audit
Deliver: content-addressed artifact references, provenance and redaction, revision-aware supersession, contradiction reconciliation and trace links.
Accept: audit flags missing evidence, mismatched verdicts and hash corruption; replay yields the same verdict history; no secrets persisted.
State: implemented; audit treats the highest-revision verdict before terminal completion as authoritative and reports lower-revision verdicts as superseded (see `tests/test_audit.py`).

### VER-04 [x] P2 — Online/offline verifier parity
Deliver: one verifier protocol for local tasks and benchmark adapters, sandboxed where required.
Accept: shared contract tests for success, failure, unavailable verifier, timeout and inconsistent evaluator responses; fail closed when verification is unavailable.
State: implemented; both runner paths record an `unavailable` verdict with a safe error class and perform normal teardown.

### VER-05 [~] P1 — Harness observation contract
Deliver: a typed `ObservationRecord` (producer, capture time, world revision, artifact hash)
produced only by Harness-owned sources - the platform verdict and Harness-side probes - with the
Agent's `expected_observation` no longer acting as the comparison baseline: no observation
records `unavailable` and never steers, a recorded observation is compared.
Accept: unit tests for "no observation, no steer", a matched observation, and a forged producer
being rejected; the VER-01/02 authority model and the claim < evidence < verified ladder are
unchanged.
State: implemented. `ObservationRecord` is a typed, frozen record (producer limited to
`harness_probe` / `benchmark_evaluator`, capture time, world revision, kind, summary, artifact
hash); `ActionVerifier.verify` takes the run's observations and compares against the newest one at
or before the current revision instead of the Agent's text. No observation yields the new
`unavailable` status with reason `observation_unavailable`, `replan_required=False` and no steer;
an observation yields `pending` + `observation_recorded` with the observation attached. Pending and
unavailable verdicts no longer ask for a replan - only a contradicted effect, a failed execution or
stale trusted evidence do. The benchmark runner records the platform's submission verdict as the
run's first observation source, so the next action verdict has a real baseline, and the ledger and
audit tally the new status. Evidence: `tests/test_action_verifier.py` (baseline from an
observation, foreign-run observation ignored, agent text alone unavailable),
`tests/test_benchmark_streaming.py` (a rejected candidate becomes the baseline for the next
action), `tests/test_audit.py`. Trust model unchanged: VER-01/02 still gate `verified`, and an
observation is not evidence. Outstanding: Harness-side probes beyond the platform verdict (EXE-08
phase one) and the live re-run. Evidence: 656 of 656 verdicts were `pending` with
`world_revision_changed` / `expected_observation_missing`, i.e. the Harness compared an Agent
string against a world it could not see.

**Not in scope:** LLM self-confidence as proof, automatically elevating World claims, benchmark-specific shortcuts in core.

