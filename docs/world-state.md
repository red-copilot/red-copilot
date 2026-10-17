# Universal World State Runtime

The Harness keeps durable, replayable run state without owning the Agent's
reasoning. The Agent writes its own markdown notes; the World is a fact and
evidence projection assembled by the Harness.

## Ownership

- Agent memory: `$HARNESS_NOTES_DIR`, plus `shared-findings.md` for competing
  sessions. The Harness does not parse, rank or rewrite either file.
- Harness observations: targets, deterministic facts extracted from bounded
  tool output, benchmark submission feedback and operator hints.
- Authority: only a task verifier or benchmark evaluator can mark the objective
  verified. Agent text and tool exit codes remain observations.

The Agent no longer submits typed action-intent or world events. There is
no Agent-facing World inbox or event file.

## Core objects

- `Entity`: a host, endpoint, process, principal, resource or other world object.
- `Relation`: a typed directed relation between objects.
- `Observation`: a Harness-recorded fact with provenance and trust tier.
- `Artifact`: a durable or addressable object.
- `Capability`: an ability acquired within a scope.
- `Hypothesis`: a testable belief; retained for verifier and legacy-run compatibility.
- `Goal`: the immutable run objective and final status.
- `ActionRecord`: execution bookkeeping used by audit and recovery.
- `Constraint`, `Failure`: execution boundaries and failed attempts.

Core type strings are intentionally open. The state is not assumed to be a DAG.

## Persistence

The SQLite event stream in `world.db` is canonical. `world.events.jsonl` and
`world.snapshot.json` are compatibility/debug exports. Every mutation is an
event; the reducer derives the snapshot.

Container Agents never write these files. The run root and canonical database
stay outside every Agent mount.

## Context projection

`WorldContextBuilder` writes `world.context.txt` with:

- the objective and remaining budget;
- target entities and Harness-extracted evidence;
- open hypotheses and bounded failure records where present;
- pointers to notes, shared findings and the summary file.

The projection does not contain a Focus block, a subgoal, a candidate action or
a next-action recommendation. It is a deterministic fact view, not a plan.

## Trust and history

Every record carries provenance: actor, source, event identity, epistemic
status, observation time and validity window. `claim` < `evidence` < `verified`.
Agent-authored text never becomes verified evidence.

Historical runs may still contain `agent.events.jsonl`, `world.inbox.jsonl`,
old progress fields and `action.verified` trace events. They remain read-only
audit inputs; new runs neither create nor consume them.
