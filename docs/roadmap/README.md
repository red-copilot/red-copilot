# Harness subsystem roadmaps

Architecture target: an **evidence-driven, resumable Python modular monolith**. Pi owns agent reasoning; Harness owns lifecycle, execution policy, world records, verification, budgets, traces and recovery. No special treatment for TSec. Status syntax: `[ ]` pending, `[~]` in progress, `[x]` verified complete, `[!]` blocked or descoped with reason, `experimental` not started. These files list item definitions, acceptance criteria and current state; a checkbox is not proof that behaviour is absent.

## Workstream index

| ID | Roadmap | Priority | Dependencies |
|---|---|---|---|
| VER | [Verification & evidence](verification.md) | P0 | none |
| RUN | [Runtime & recovery](runtime.md) | P0 | VER contracts |
| EXE | [Agent & execution isolation](execution.md) | P0 | RUN events |
| WLD | [World state & retrieval](world.md) | P1 | VER provenance |
| PLN | [Planning & skills](planning.md) (mostly descoped by the v2 cut) | P1 | WLD |
| BEN | [Benchmark, evaluation & offline](benchmark.md) | P1 | VER, EXE |
| QA | [Quality, architectural boundaries & performance](quality.md) | continuous | all |

The agent-side design (cognition layer, competing sessions, competition constraints and the
pre-event P0/P1 ladder) is [agent-engineering.md](../agent-engineering.md); this index stays the
source of truth for subsystem item status.

## Delivery stages

- **S0 correctness:** VER-01/02, RUN-01/02, EXE-01, QA-01. Objective success rests on independent evidence and recoverable durable control state.
- **S1 bounded autonomy:** RUN-03, WLD-01/02, BEN-01/02. State changes affect the next action; no unsafe retry or network assumptions. (PLN-01 was descoped: the Agent owns replanning.)
- **S2 measurable generality:** BEN-03, QA-02/03, PLN-04 (competing sessions, done). Improvements demonstrated across domains, not on one benchmark.
- **S3 optional experiments:** learned retrieval, planning or alternate stores only if S2 ablations show a measured gap.

## Global acceptance gates

1. Verification: no Agent-authored string or tool exit status alone can mark an action effect or goal verified; each verdict references independent recorded evidence.
2. Recovery: crash points before/after dispatch, effect, feedback and checkpoint do not silently replay ambiguous side effects.
3. Isolation: the Agent cannot write the canonical database, scoring results or secrets; egress policy is explicit.
4. Replay/audit: deterministic reduction of authoritative events; conflicting IDs, missing provenance and corrupt checkpoints are surfaced.
5. Architecture: domain modules never import API, CLI, TSec SDK or Docker implementations; adapter contracts are tested; module-count and source-line ceilings hold.

## Working agreement

Identify an unblocked item, inspect the current tree, write focused tests, implement, run the targeted plus full suites, record evidence, then update status only after acceptance. Request maintainer review for changes to trust boundaries or public contracts.

