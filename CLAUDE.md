# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read First

[`AGENTS.md`](AGENTS.md) is the authoritative contributor guide (boundaries, gotchas, delivery
rules). This file summarizes the architecture and commands; when they disagree, `AGENTS.md` and
the tests win. Before a task, open [`docs/roadmap/README.md`](docs/roadmap/README.md), the
relevant subsystem roadmap, and [`docs/architecture.md`](docs/architecture.md). Read
[`docs/security/execution-boundary.md`](docs/security/execution-boundary.md) and
[`docs/security/gateway-tools.md`](docs/security/gateway-tools.md) before touching isolation,
egress, or the gateway.

## Commands

```bash
python -m pip install -e ".[dev]"        # Python 3.11+, pytest + ruff + hypothesis
python -m pip install -e ".[tsec]"       # optional TSec SDK

ruff check .                              # lint (line-length 100)
pytest -q                                 # full suite
pytest tests/test_budget.py -q            # one file
pytest tests/test_budget.py::test_name -q # one test
pytest tests/test_architecture.py -q      # run after adding/moving/deleting any module
```

Environment-dependent tests (Docker, bubblewrap + prlimit, POSIX signals) skip themselves, so a
green local `pytest -q` does not prove the CI-only Docker/Pi paths. Exercise those explicitly
when you change them.

```bash
docker build -t harness/pi-kali:local docker/pi-kali
harness capabilities                      # what this machine can actually run
harness validate benchmarks/examples/hello/task.yaml
harness preflight <task.yaml> --agent <agent.yaml>
harness run benchmarks/examples/hello/task.yaml --agent agents/examples/pi.yaml --seed 42
harness report <run_dir>                  # summarize result.json + artifacts
harness audit-run <run_dir>               # read-only consistency check (exit 0/1/2)
harness hint <run_dir> --text "..."
harness subagent --objective "..."        # isolated Pi sub-agent; its text is unverified
harness tsec --agent agents/examples/pi-tsec.yaml [--challenge ID | --all]
harness gc-runs --root .harness/tsec-runs [--apply]
harness gateway                           # requires HARNESS_GATEWAY_TOKEN
```

A TSec sweep gates itself on VPN/control-plane readiness: exit 3 means fix the platform (never
wrap the sweep in a retry loop), exit 4 means some case produced no run. Credentials come only
from the environment (`BENCHMARK_BASE_URL`/`BENCHMARK_TOKEN`,
`HARNESS_GATEWAY_TOKEN`/`HARNESS_MODEL_API_KEY`); never put them in YAML, traces, or `.harness/`.

## Architecture

One deployable Python application, a modular monolith under `src/harness/`. The organizing
principle: **the Agent owns cognition; the Harness owns environment, trust, accounting,
recovery and verification.** There are two nested loops:

```
Agent inner:   observation -> Pi reasoning -> tool/model -> environment -> observation
Harness outer: constraints -> admission/budget -> dispatch -> actuator/plant
                -> independent sensor -> evidence/verdict -> continue/finalize/deny/recover
```

Only an authorized verifier or benchmark evaluator may close the objective loop. Agent claims,
tool exit codes, and "looks done" never count as evidence.

### Entry points and composition

- `cli.py` (Typer app `harness`) dispatches every command.
- `orchestrator.py` owns local task runs; `benchmark/runner.py` owns externally provisioned
  benchmark runs (`discover -> provision -> run -> submit -> evaluate -> teardown`).
- Both converge on `runtime/bootstrap.py::bootstrap_solver`, the single composition root for
  World, Progress, Trace, VerifierRegistry and `SolverLoop`. Do not duplicate that wiring.
- `solver_loop.py` drives the interactive session; `solver_policy.py` is the pure external stop
  law (budgets/wall clock). The Harness never stops a run because it inferred repetition or
  lack of progress.
- `models.py` holds the pydantic contracts (task/agent/policy) and is authoritative; `schemas/`
  covers trace events only.

### Package map

| Path | Responsibility |
| --- | --- |
| `runtime/` | Composition, checkpoints/reconciliation, lifecycle status, result persistence, executor-session plans |
| `world/` | Event-sourced domain state, SQLite store (`world.db`), projections, retrieval, focus/context rendering |
| `benchmark/` | Adapter protocol plus the TSec adapter; no web/Docker imports |
| `kernel/` | Narrow causal write ports: `LedgerAppender` and `ActionAuthority` (durable admission/dispatch/reconciliation) |
| `agent.py`, `session.py`, `pi_adapter.py`, `pi_container.py` | The only Agent path: Pi RPC inside hardened Kali Docker |
| `gateway.py`, `gateway_runtime.py`, `tool_adapter.py` | Host model proxy + tool authorization; credential boundary |
| `ledger.py` | Append-only causal-ledger shadow (v2 trace remains authoritative) |
| `progress.py`, `budget.py`, `extract.py`, `hints.py`, `trace.py`, `audit.py` | Accounting, stop law, submission extraction, operator hints, traces, audit |

`src/spike_swarm/` and `spike/` are a deliberately disconnected experiment: they import nothing
from `harness`, are unreachable from the CLI, and are not wired into CI. Do not couple them to
the production package.

### Enforced boundaries

`tests/test_architecture.py` parses imports without executing modules and enforces:

- One-way dependencies: `world/` may not import `benchmark/`, `runtime/`, `orchestrator`,
  `cli`, `gateway`, `gateway_runtime`; `runtime/` may not import `benchmark/`, orchestrator,
  CLI, gateway; `benchmark/` may not import orchestrator, CLI, gateway.
- `world/` and `runtime/` may not import `fastapi`, `starlette`, `docker`, `subprocess`,
  `httpx`, or `tsec_benchmark`; `benchmark/` may not import `fastapi`, `starlette`, or `docker`.
  Nested imports are checked, including deferred imports inside functions.
- A frozen import closure (`CONTROL_SPINE`) rooted at `runtime.bootstrap`, `benchmark.runner`
  and `solver_loop`. New reachable modules must be added to that set deliberately.
- `DELETED_SUBSYSTEMS` (removed open-loop and retired host paths) must never reappear, including
  through dynamic imports or `eval`/`exec`.

### Trust and state invariants

- Pi is the only Agent execution path and always runs in a restricted container over RPC. There
  is no host Agent path. `verification.type: python` still executes a trusted host process under
  bubblewrap.
- Agent containers get read-only `/task` and `/knowledge/<name>`, and a writable workspace only.
  They must never write the run root, `world.db`, results, traces, checkpoints, evidence, or
  secrets. Treat all benchmark, model, tool, and Agent output as untrusted input.
- Operator hints land in `hints.jsonl` at the run root, unreachable from the container; they can
  steer but never verify.
- `AgentSpec.executors` runs up to four competing sessions per case. Each gets its own container,
  workspace, notes, progress and checkpoint under `run_dir/executors/<nn>-<id>/`; they share one
  provision, World, submission dedupe set and result. `executor-shared/` is the only writable
  cross-session path and must never contain authoritative artifacts.
- World state is event-sourced; `world.db` is authoritative and `world.events.jsonl` /
  `world.snapshot.json` are compatibility/debug exports. `progress.json` is a separate
  claim/accounting ledger and never decides stop/steer.
- The causal ledger is a shadow (`harness/ledger.py`): it mirrors trace events, never changes
  `result.json` or run status, and can be disabled with `HARNESS_LEDGER_SHADOW=0`.

Each run directory under `.harness/runs/<run_id>/` is the record: `result.json`, `trace.jsonl`,
`events.jsonl`, `world.db`, checkpoints, agent/verifier logs, and `summary.json`. Inspect it with
`harness report` / `harness audit-run`, not by hand.

## Conventions and Gotchas

- CI's `enforce harness namespace` step scans every tracked file for legacy pre-rename
  identifiers, including quoted dotted module-path strings. The exact tokens are in
  [`.github/workflows/ci.yml`](.github/workflows/ci.yml); do not restate them in any tracked
  file (a mention fails the build just as hard as real usage).
- Docs mix languages: `docs/agent-architecture-v2.md`, `docs/agent-engineering.md`,
  `docs/architecture-map.md` and the Chinese sections of `README.md` are Chinese; write in the
  language of the file you edit.
- Backward-compatible import paths and event schemas are preserved until a versioned migration
  and tests exist. Pydantic models are the contract source of truth.
- Commits use Conventional Commits with a scope (`feat(runtime): ...`, `fix(pi): ...`). Unless
  the user says otherwise, once work is verified, commit, push the feature branch, merge into
  the latest `main` (never force-push), and push `main` — but stop for maintainer review on
  public contracts, credentials, network permissions, verification trust boundaries,
  destructive migrations, or evaluation semantics. Report the true SHAs.
- Roadmap status syntax: `[ ]` pending, `[~]` in progress with PR link, `[x]` verified complete
  with test/commit link, `[!]` blocked with reason. A checkbox is not proof behavior is absent;
  check code and tests.
- `.env` and `*.ovpn` are gitignored local run inputs — never commit them, and don't delete them
  as stray files.
