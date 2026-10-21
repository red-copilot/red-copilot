# ADR 0001: Add a causal ledger in shadow mode

- **Status:** Accepted
- **Date:** 2026-10-22
- **Scope:** Migration stages M0 and M1
- **Related design:** [First-principles architecture v3](../superpowers/specs/2026-10-22-first-principles-architecture-design.md)

## Context

v2 keeps cognition in the Agent and the environment, trust, accounting and recovery in the
Harness. Its authoritative state is spread across `trace.jsonl`, World events, `progress.json`,
checkpoints and `result.json`. That split is workable, but it cannot answer one causal question
without reconstructing several mutable files: which authorized action produced which observation,
and which observation supported the objective verdict?

The v3 design makes one append-only causal ledger the future source of truth. Switching the
production read path immediately would combine an unmeasured write path with a trust-boundary
change. This ADR therefore freezes only the ledger contract and M1 shadow implementation. v2 remains
authoritative, and M2 is not started.

## Immutable assumptions

1. Agent output can be wrong, incomplete or misleading and is untrusted input.
2. The Harness can rely only on observations actually captured by an authorized producer.
3. An in-flight side effect may have happened even when its response was lost.
4. Claims, tool execution, observed effects and objective verdicts are different facts.
5. Mutable state can drift after a crash, concurrent access or manual intervention.
6. The Agent explores; deterministic Harness code enforces authority, accounting and termination.

## Decision

Each Run may have one append-only SQLite ledger beside its v2 artifacts. In M1 the ledger is a
shadow: `TraceRecorder` writes the v2 trace record first and then mirrors the same event into the
ledger. World, Context, Result, Progress and Checkpoint reads remain unchanged.

### Store contract

- Store schema: `harness/ledger-store/v1`.
- Event schema: `harness/ledger-event/v1`.
- Location: `ledger.db` in the Run directory.
- SQLite uses WAL, `synchronous=FULL`, a five-second busy timeout, one in-process writer lock and
  `BEGIN IMMEDIATE` for appends.
- `ledger_meta(key, value)` stores the store schema version.
- `ledger_records` is append-only. `BEFORE UPDATE` and `BEFORE DELETE` triggers abort both
  operations.
- `event_id` is unique. Re-appending identical event content returns the existing record; reusing an
  `event_id` with different content raises `LedgerConflict`.
- `ledger_seq` is monotonic within a Run. Reopening an existing ledger continues its sequence and
  hash chain rather than resetting either.

### Record contract

Every record has exactly these logical fields:

```text
ledger_seq, event_id, run_id, attempt_id, producer, trust_tier, kind,
schema_version, occurred_at, causation_id, correlation_id, payload,
artifact_refs, prev_hash, record_hash, content_hash
```

`payload` and `artifact_refs` are persisted as canonical JSON. `content_hash` covers the immutable
event content except `ledger_seq` and the hash fields. `prev_hash` starts at 64 zeroes and otherwise
names the previous `record_hash`. `record_hash` covers `ledger_seq`, `content_hash` and `prev_hash`.
Verification checks contiguous sequence numbers and both hash rules.

The hash chain detects accidental corruption and projection drift. It is **not a cryptographic
proof against a malicious host**: host code and host administrators can rewrite the database or
replace the implementation. The chain must not be described as tamper-proof or as a distributed
consensus mechanism.

### Trust tiers

The M1 event adapter records one of three trust tiers:

- `untrusted`: producers whose name starts with `agent`.
- `external`: the `gateway` producer.
- `authorized`: Harness and verifier-style producers handled by the deterministic adapter.

The tier is provenance metadata, not a substitute for authority checks. In particular, no claim
event can project to an objective verdict. Only a typed `objective.verdict` sets
`objective_status`. Producer capability and schema validation remain part of the later production
kernel migration.

### v2 trace mapping

| v2 trace field | ledger field |
| --- | --- |
| `event_id` | `event_id` |
| `run_id` | `run_id` |
| `data.executor_id`, else `data.session_id` | `attempt_id` |
| `data.producer`, else `actor` | `producer` (then classified into `trust_tier`) |
| `type` | `kind` |
| `ts` | `occurred_at` |
| `parent_event_id` | `causation_id` |
| `action_id`, else `data.tool_call_id`, else `data.executor_id` | `correlation_id` |
| `data` | `payload` |
| `data.artifact_ref`, `data.artifact_refs`, nested evidence refs | `artifact_refs` |

The shadow replay derives terminal state from `run.finished`, objective status from
`objective.verdict`, actions from `tool.call` and `tool.result`, budget usage from model, tool and
denial events, and submission counts from benchmark submission events.

### Shadow flag and failure isolation

`HARNESS_LEDGER_SHADOW` controls M1. It is enabled by default so ordinary Runs exercise the
migration. The values `0`, `false`, `no` and `off` disable it.

A ledger failure must never change the Run outcome or `result.json`. The v2 trace line is written
before the shadow append. The first ledger failure disables further shadow attempts for that
recorder, emits a redacted `ledger.error.json`, and is logged as a warning. Terminal comparison runs
only after the atomic v2 result write and is best-effort.

At terminal persistence, replay writes `ledger.comparison.json` with schema
`harness/ledger-comparison/v1`. It compares terminal status/success/score, objective status, action
identity and active states, budget counters and attempted submissions against the authoritative v2
projections. A mismatch is diagnostic evidence for M2; it does not alter the v2 result.

## Rollback

Set `HARNESS_LEDGER_SHADOW=0` to return a process to the v2-only write path. Existing `ledger.db`,
`ledger.comparison.json` and `ledger.error.json` artifacts are non-authoritative and may be retained
for analysis or removed with the Run directory. No schema downgrade or production data migration is
required. Code rollback also requires no v2 state change because no v2 read path consumes the
ledger.

## M1 status and exit criteria

M1 is implemented in shadow mode. M2, which would make ledger projections production inputs, is not
started. M2 requires evidence that shadow replay matches representative v2 Runs, that failures are
isolated, and that recovery preserves sequence and hash-chain continuity. Real sweep, crash-matrix
and projection-rebuild coverage remain later migration gates.

## Consequences

- The control spine gains one deliberate module, `harness.ledger`.
- The measured package ceiling is 58 modules and 16,685 source lines.
- Runs carry a second local artifact store while the comparison remains enabled.
- v2 behavior and trust boundaries remain intact during the observation period.
- The ledger is useful for auditability only after its replay semantics are measured; it is not yet
  the system of record.

## Non-goals

- No production read path consumes the ledger in M1.
- No exactly-once guarantee, distributed transaction, message queue or microservice.
- No cryptographic defense against host administrators or arbitrary database rewrites.
- No replacement of verifier/evaluator authority with Agent claims or tool exit codes.
