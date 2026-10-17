# Blackboard protocol reference

The runtime renders one shared graph per challenge each round. Three solvers
read it and contribute through their own files, so there is no cross-container
write contention and no solver can rewrite another solver's contribution.

| Path (container) | Mode | Owner | Purpose |
| --- | --- | --- | --- |
| `/board/inbox/<session>.md` | rw | that session only | `fact:` / `intent:` / `question:` / `flag:` / `note:` lines |
| `/board/goals/<session>.jsonl` | rw | that session only | goal ops (`propose`, `status`) |
| `/board/intent-fact-graph.md` | ro | runtime | the merged graph rendered from every inbox |
| `/notes` | rw | that session only | working notes |
| `/workspace` | rw | that session only | scratch space and Pi session state |
| `/knowledge/*` | ro | operator | skills, methodology and this file |

## Merge rules

- Lines are merged across sessions; facts with the same normalised text collapse
  into one entry that records every contributing session. More contributors rank
  higher, and the most confident statement wins.
- Intents with the same text and target collapse; the most advanced status wins
  (`done` > `doing` > `open` > `dropped`).
- Anything that does not match the grammar is kept verbatim under "Unparsed
  lines" so a malformed line is visible instead of silently dropped.
- Only new goal-op lines are applied, and only by the runtime. Re-applying a
  proposal or re-closing a terminal goal is rejected and recorded in `goal.jsonl`.

## What the runtime records

Each round it snapshots the rendered graph with a content hash. If the file on
disk no longer matches what the runtime last wrote, that is recorded as an
incident (`clobber` when it shrank, `modified` otherwise) in `result.json`.
