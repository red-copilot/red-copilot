# WLD — World state and retrieval

**Scope:** `world/`, `aci.py`. The SQLite event repository, reducers, inbox ingestion, provenance labels, snapshots and deterministic retriever exist; World is not assumed to be a DAG.

### WLD-01 [x] P1 — State integrity and semantics
Deliver: event authority, revision CAS and idempotence plus provenance tiers for claim/evidence/verified, validity windows, supersession and contradiction, with migration tests.
Accept: deterministic replay, append/inbox duplication, concurrent-writer conflict, stale evidence and malicious trust-escalation tests.
State: implemented; actor-derived tiers, timezone-aware validity windows, revision CAS, idempotent append, conflict detection and deterministic replay are covered by `tests/test_sqlite_world.py`.

### WLD-02 [x] P1 — Decision-useful context
Deliver: query current goals, valid capabilities, blockers, contradictions, evidence references and relevant failures within a bounded prompt budget, with deterministic output and disclosure of missing knowledge.
Accept: golden retrieval fixtures, no stale capability promoted, context staying within budget and changing when evidence status changes.
State: implemented via `WorldRetriever` and `WorldContextBuilder` with validity filtering, contradiction links and empty-section disclosure.

### WLD-03 [x] P2 — Compact storage, query and snapshots
Deliver: retention and compaction without losing provenance, indexed SQLite queries and periodic snapshot/restore.
Accept: replay parity after compaction, large synthetic runs within the stated latency and storage budgets, corruption detection.
State: implemented; the 1,000-event fixture stays under 10s, 32 MiB traced allocation and 20 MiB stored bytes with replay/export/snapshot parity checks, backup/restore and corrupt-snapshot detection. The JSONL store was removed; SQLite is the only implementation.

### WLD-04 [ ] experimental — Learning-based retrieval
Deliver: only propose after WLD-02 ablations establish retrieval bottlenecks, comparing against the deterministic baseline on held-out domains.
Accept: measurable success gain relative to cost, reproducible offline model availability and a rollback path.

### WLD-05 [~] P2 — Token-budget-aware context assembly
Deliver: bound `world.context.txt` (focus plus state projection) by an offline token estimate instead of character counts alone.
Accept: rendered context stays within the configured token budget with the focus share preserved; `None` keeps the legacy character budgets byte-identical.
State: implemented (`WorldContextBuilder.token_budget`, `estimate_tokens`, `--context-token-budget`); a live-run ablation remains before acceptance.

**Non-goals:** full causal simulator, mandatory graph planner, vector database without measured value.

