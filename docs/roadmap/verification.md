# VER — Verification and evidence

**Scope:** `verifier.py`, `audit.py`, World provenance. Agent observations stay claims; only
structured trusted evidence can complete an objective.

### VER-01 [x] P0 — Typed evidence and verdict contracts
Deliver: immutable `EvidenceRef`/`ObjectiveVerdict` carrying run IDs, producer, capture time,
world revision and artifact hash, with command execution and objective completion kept distinct;
non-Harness producers rejected.
Accept: unit tests for forged producer, missing action ID, stale revision, conflicting evidence and a failed tool with apparent success text; no false verified verdicts.
State: implemented in `verification.py`. RUN-09 removed action-level verdicts; the objective
verdict remains the sole success authority.

### VER-02 [x] P0 — Trusted verification boundary
Deliver: `VerifierRegistry` authority for `tool_adapter`, the isolated task verifier and the benchmark evaluator, enforced at the integration boundary rather than by a caller-supplied dictionary.
Accept: an integration test with malicious Agent claims and successful command exits failing goal verification; terminal verified goals tie to durable evidence IDs.
State: implemented; Python task verifiers run under bubblewrap with namespaces, no capabilities,
read-only inputs and resource limits, failing closed when unavailable. Tool exits and Agent text
cannot issue a `verified` objective verdict.

### VER-03 [x] P1 — Durable evidence lifecycle and audit
Deliver: content-addressed artifact references, provenance and redaction, revision-aware supersession, contradiction reconciliation and trace links.
Accept: audit flags missing evidence, mismatched verdicts and hash corruption; replay yields the same verdict history; no secrets persisted.
State: implemented; audit treats the highest-revision verdict before terminal completion as authoritative and reports lower-revision verdicts as superseded (see `tests/test_audit.py`).

### VER-04 [x] P2 — Online/offline verifier parity
Deliver: one verifier protocol for local tasks and benchmark adapters, sandboxed where required.
Accept: shared contract tests for success, failure, unavailable verifier, timeout and inconsistent evaluator responses; fail closed when verification is unavailable.
State: implemented; both runner paths record an `unavailable` verdict with a safe error class and perform normal teardown.

### VER-05 [!] P1 — Action observation contract (superseded by RUN-09)
Descoped: the Harness no longer derives action verdicts from Agent-declared expected
observations. The 2026-10-16 sweep produced 656/656 `pending` action verdicts, so RUN-09 removed
the action verifier. Tool output remains observation input; the objective verifier or benchmark
evaluator remains the only success authority.

**Not in scope:** LLM self-confidence as proof, automatically elevating World claims, benchmark-specific shortcuts in core.

