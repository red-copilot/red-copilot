# Pi-swarm spike

Standalone experiment: **three challenges concurrently, three Pi solvers each,
one runtime-rendered intent-fact graph per challenge.**

This directory holds the operator documentation and the agent-facing assets. The
code lives in `src/spike_swarm/` and imports nothing from `harness`. It is not
reachable from the `harness` CLI, is not wired into CI, and is meant to be
disposable: it exists to answer one question, whether a shared blackboard plus a
runtime-owned goal machine changes what three Pi agents can do on a live TSec
challenge.

## What runs

- One sweep = up to three TSec challenges in parallel (the platform caps a team
  at three running instances).
- One challenge = one provisioned instance + three Pi containers, each with its
  own workspace, notes, inbox and goal-op file. The three sessions differ in
  thinking budget (`high`, `medium`, `low`) and share one model and provider.
- The runtime owns container lifecycle, round boundaries, the merged graph, the
  goal state machine and the only platform credential. Solvers never see the
  platform token.
- Selection defaults to the three easiest unfinished challenges; `--challenge`
  overrides it.

## Prerequisites

- The Benchmark VPN is up and the worker can reach the control plane.
- `BENCHMARK_BASE_URL` and `BENCHMARK_TOKEN` are exported (names configurable
  with `--base-url-env` / `--token-env`).
- The solver credential is exported and named with `--passthrough`
  (for example `OPENAI_API_KEY`); values never reach the artifacts or the
  container argv.
- The Kali image is built: `docker build -t harness/pi-kali:local docker/pi-kali`.
- Machine budget: `--parallel` x 3 containers, so the default sweep needs nine
  containers plus the platform's three-instance quota.
- The `tsec-benchmark` SDK is importable in the environment running the sweep.

## Usage

```bash
python -m spike_swarm sweep \
  --count 3 --parallel 3 \
  --passthrough OPENAI_API_KEY \
  --wall-time 3600 --round-seconds 180
```

Pick the challenges explicitly, or preview the plan without touching the
platform:

```bash
python -m spike_swarm sweep --challenge WEB-001 --challenge WEB-002 --challenge CRYPTO-003
python -m spike_swarm sweep --challenge WEB-001 --plan-only
```

Exit codes: `0` every challenge produced a run, `2` bad invocation, `3` a
platform-wide failure (VPN, unreachable, instance conflict) - fix the platform,
do not retry in a loop - and `4` a challenge produced no run.

## Round loop

1. Merge every inbox into one board snapshot.
2. Render `shared/intent-fact-graph.md` (and snapshot it with a content hash).
3. Apply new goal ops from `shared/goals/<session>.jsonl`; only the runtime
   writes `goal.jsonl`.
4. Submit any new flag candidate through the platform; an accepted flag ends the
   task and tears the sessions down.
5. Brief all three solvers at once - goal state, graph and their last status -
   and wait for each turn to settle or for the round to time out.

A subgoal only reaches `achieved` when the cited fact is already on the merged
graph and a *different* session contributed it first. The root goal closes only
on a platform-accepted flag.

## Artifacts

```text
.spike/runs/<challenge>/
  result.json          verdict, sessions, flags, goal summary, blackboard stats
  goal.jsonl           append-only goal journal (runtime is the only writer)
  platform.jsonl       start / submit_flag / close transcript, no credentials
  shared/
    inbox/<session>.md   solver-authored lines
    goals/<session>.jsonl goal ops
    intent-fact-graph.md runtime-rendered graph
    snapshots/           per-round render with its hash
  sessions/<nn>-<id>/
    workspace/  notes/  pi.stdout.log  pi.stderr.log  status.jsonl
.spike/runs/sweep.json    selection, per-task summary, exit code
.spike/runs/platform.jsonl every platform call of the sweep
```

`.spike/` is gitignored. Artifacts are the evidence: a finished sweep should show
a rendered graph per round, at least one fact contributed by one solver and read
by the others, goal transitions in `goal.jsonl`, and a flag transcript.

`goal.jsonl` is authoritative for one attempt, so running the sweep again on the
same challenge first moves the previous `result.json`, `goal.jsonl`,
`platform.jsonl`, `shared/` and `sessions/` into `archive/<utc>/`. Nothing is
replayed into a new attempt.

The agent contract and the blackboard protocol in `spike/agent/` are mounted
read-only at `/knowledge/spike`, alongside `skills/offensive` and `knowledge`.
