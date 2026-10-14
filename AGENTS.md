# Repository Guidelines

## Project Structure & Module Organization
`src/harness/` is the Python package (a modular monolith). Subpackages: `api/` (FastAPI
control plane), `benchmark/` (adapter contracts and TSec integration), `world/`
(event-sourced state), `runtime/` (lifecycle, checkpoints, evidence). Top-level modules
include `cli.py`, `orchestrator.py`, `agent.py`, `gateway.py`. Tests live in `tests/` as
`test_<module>.py`. Fixtures and baselines sit under `benchmarks/examples/` and
`benchmarks/baselines/`; agent configs under `agents/examples/`; JSON Schema contracts in
`schemas/`; Dockerfiles in `docker/`; architecture and roadmap docs in `docs/`. Domain
modules must not import API/CLI/Docker code: `tests/test_architecture.py` enforces this.

## Build, Test, and Development Commands
- `python -m pip install -e ".[dev]"` - editable install with pytest and ruff (Python
  3.11+); add `.[tsec]` for the optional TSec SDK.
- `ruff check .` - lint. `pytest -q` - full test suite.
- `harness run benchmarks/examples/hello/task.yaml --agent agents/examples/demo.yaml
  --allow-host-agent --seed 42` - run one task.
- `harness suite benchmarks/examples/smoke-suite.yaml --agent agents/examples/demo.yaml
  --allow-host-agent --workers 2` - run a suite in parallel.
- `harness validate`, `harness validate-suite`, `harness preflight` - check task, suite,
  and agent contracts.

Prerequisites: Docker for Docker agents, sidecar Gateway, and Docker verifiers;
`sudo apt-get install -y bubblewrap` for the verifier sandbox; Pi 1.0.4 via
`npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.0.4` (Node 22.19+);
gVisor only when a run sets `runtime: runsc`.

## Coding Style & Naming Conventions
Python 3.11+, 4-space indentation, `from __future__ import annotations`, and type hints on
public signatures. Use `snake_case` for modules, functions, variables, `PascalCase` for
classes, and `UPPER_SNAKE_CASE` for constants. Ruff is the only enforced tool
(`line-length = 100`, per `pyproject.toml`); no formatter is enforced, so match surrounding
style. YAML uses `apiVersion: harness/v1`; shared contracts live in `schemas/`.

## Testing Guidelines
Tests use pytest and follow `tests/test_*.py` with `test_*` functions;
`testpaths = ["tests"]` in `pyproject.toml`. Run a targeted file first
(`pytest tests/test_budget.py -q`), then the full `pytest -q`. Behavior changes need
positive and negative cases; existing tests assert fail-closed handling of malformed input.
No coverage threshold is configured. Tests requiring Docker, Pi, or bubblewrap should note
environment-dependent exclusions in the PR.

## Commit & Pull Request Guidelines
Commits follow Conventional Commits with a scope: `feat(runtime): ...`, `fix(pi): ...`,
`docs(test): ...`, `refactor:`, `test:`, `style:`, `ci:`. Reference issue or PR numbers
(for example, `(#9)`) when relevant. Keep a PR to one roadmap item, describe the scope,
list the commands run and their results, link the issue or roadmap entry, and note
environment-dependent exclusions. Request review for trust-boundary or public-contract
changes.

## Codex Operating Contract
Start with [docs/roadmap/README.md](docs/roadmap/README.md), then read the subsystem roadmap relevant to the requested task, docs/architecture.md, and existing tests. This document is guidance, not permission to run autonomously outside a requested task or to merge without review.

### Iteration algorithm
1. Inspect main and existing behavior; select the highest-priority **unblocked** roadmap item. Do not assume an item is incomplete without checking code/tests.
2. Open a focused branch/PR. Prefer one roadmap item per PR; avoid unrelated refactoring.
3. Write a failing regression or acceptance test first when feasible, then implement the smallest fix.
4. Execute targeted tests, architecture tests, lint, and the full feasible suite. Note commands, failures, and environment-dependent exclusions in PR.
5. Update the item status/evidence in its roadmap, with links to tests/PRs and measured before/after impact. Mark done only on demonstrated acceptance criteria.
6. Review invariants: untrusted Agent output cannot become verified evidence; retries cannot duplicate unsafe effects; benchmarks cannot leak into core; agent containers cannot mutate authoritative state.
7. Halt and request maintainer review if the task changes public contracts, destructive data migrations, credentials, network permissions, verification trust boundaries, or evaluation semantics.

### Engineering rules
- Python modular monolith. No premature microservices, mandatory DAG, neural world model, or vector database.
- Keep the Pi agent's reasoning loop separate from Harness run control. Domain state in World; ephemeral control state in Runtime; objective verdicts in Verification.
- Preserve backward-compatible import paths and event schemas until a versioned migration and tests exist.
- Keep TSec integration under benchmark adapters. Test offline operation; no install-at-runtime assumptions.
- Claims, tool exits, action-effect verification and goal completion are distinct.
- Reconcile unknown in-flight actions before retrying. Never claim exactly-once execution without an atomic protocol supporting it.
- Treat container, benchmark, model-provider and tool outputs as untrusted. Never put credentials in traces/artifacts.
- Compare baseline metrics before introducing complexity. Roll back changes that worsen reliability without compensating benefits.

### PR checklist
- [ ] Scope and roadmap item ID identified
- [ ] Acceptance tests and negative/security cases added
- [ ] Targeted tests and CI checks reported accurately
- [ ] Recovery, replay, idempotency and offline behavior considered
- [ ] Documentation/status updated with evidence
- [ ] No unauthorized target activity, secrets, or hidden external dependencies

### Status syntax
`[ ]` pending; `[~]` in progress (link PR); `[x]` verified complete (link commit/test); `[!]` blocked with reason. Roadmaps describe *planned* work, not proof that existing implementation is absent.
