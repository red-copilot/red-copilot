# Modular monolith boundaries

Harness is one deployable Python application, not a collection of microservices.
Modules are separated by responsibilities and explicit composition points.
The single closed feedback loop that these modules form is described in
[control-theory.md](control-theory.md).

- `runtime/`: application-level composition, solver initialization, run state.
- `world/`: domain state, event storage, snapshots, retrieval. No benchmark or HTTP dependencies.
- `benchmark/`: externally provisioned challenge lifecycle, submissions and adapters.
- `agent.py`, `session.py`, `pi_*.py`: agent/session integration and infrastructure.
- `tool_adapter.py`: domain-neutral async tool plugin contract; concrete tools are explicitly selected by application composition and remain subject to Gateway authorization.
- `solver_loop.py`, `planner.py`, `action_verifier.py`: solver policies.
- `orchestrator.py`: local-run lifecycle and environment integration.

The first extraction is `runtime.bootstrap_solver`. Both local runs and
externally provisioned benchmarks create the same solver state through this
composition point. Runtime does not provision environments, contact the
benchmark SDK, or run verifiers.

Dependency direction (initial migration):

```text
         CLI
          |
local orchestrator       benchmark runner
          \                /
           runtime bootstrap
              |   |   |
         solver world progress
```

New modules must not import the CLI or benchmark-specific adapters from the
domain model. Avoid exposing database connection objects across module
boundaries. Preserve public import paths until migration is complete.

The control plane, worker queue, coordination blackboard and other open-loop
subsystems were removed during the control-spine simplification; the package is
now the closed loop plus its composition points.

## Enforced dependency direction

`tests/test_architecture.py` parses package imports without importing modules.
It rejects reverse imports from world into benchmark, runtime or entry points,
from runtime into benchmark or entry points, and from benchmark adapters into
CLI, gateway or entry points. It also prevents `world` and `runtime` from
importing FastAPI, Starlette, Docker, subprocess, httpx, or the TSec SDK, and
prevents benchmark adapters from importing web-server or Docker implementations.
Negative fixtures verify that the guard detects these infrastructure
dependencies. A frozen-closure test keeps the import closure of the control
spine (`runtime.bootstrap`, `benchmark.runner`, `solver_loop`) within a declared
set, so open-loop subsystems cannot silently reappear on the loop.

CI executes the boundary check in the regular pytest suite. A boundary
exception should be explicit, documented and reviewed rather than hidden
behind runtime import aliases.

## Planned migration sequence

1. Extract shared bootstrapping (completed in this branch).
2. Introduce import-boundary tests (completed in this branch).
3. Move run-result persistence/serialization behind a runtime service (done),
   then unify local and external benchmark lifecycle states.
4. Separate SDK/network, Docker and Pi infrastructure from policy logic.
5. Consolidate composition at the CLI entry point, preserving existing external
   interfaces and the world event format.

## Lifecycle status policy

`runtime.lifecycle.resolve_run_status` centralizes the existing priority order
for externally evaluated runs: confirmed objective completion takes priority,
then timeout, budget exhaustion and ordinary finish. Local-run error and
verification branches remain in `Orchestrator`; this extraction does not
change the environment, SDK or teardown semantics. Runtime result persistence
and world projections are shared, but resource ownership stays with the
individual run entry points.
