# Red Harness

Red Harness is a reproducible evaluation runtime for security agents. It keeps benchmark tasks, agents, target environments, model/tool access, budgets, verification, telemetry, and the control loop separate so results can be independently reproduced and compared.

## Current capabilities

Version 0.8 provides:

- declarative task, agent, and policy contracts
- first-class [Pi](https://pi.dev/docs/latest) Agent Adapter running inside Kali Rolling Docker
- Pi tool/model event normalization into Harness traces and budgets
- Kali Rolling + `kali-linux-core` Pi image with curated security tools\n- isolated per-run Pi configuration and deterministic non-interactive defaults\n- generic Benchmark Adapter contracts (`discover/provision/submit/evaluate/teardown`)
- TSec Benchmark adapter over the official SDK (`list/start/submit/close`) with Harness-owned benchmark credentials
- optional Pi model routing through the Red Harness credential-isolating Gateway
- Docker Compose or no-op benchmark environments
- trusted CLI agents and restricted Docker agents
- Python or Docker-sandboxed verifiers
- host or isolated Docker-sidecar Tool/Model Gateway
- OpenAI-compatible streaming and non-streaming model proxy
- token, model-call, tool-call, cost, and wall-time budgets
- provider credential isolation
- Docker runtime profiles, including gVisor via `runtime: runsc`
- event-sourced universal world-state runtime with materialized snapshots
- domain-neutral skill metadata registry for planner and domain extensions
- rolling-horizon planner integrated with the interactive SolverLoop
- GitHub Actions coverage for Pi 1.0.4, Docker Agent, sidecar Gateway, and Docker Verifier

> [!WARNING]
> CLI agents and `verification.type: python` execute trusted host processes. Pi runs inside a restricted Docker container by default. `runtime: runsc` requires gVisor to be installed and registered with Docker.

## Install

```bash
python -m pip install -e ".[dev]"
```

Python 3.11+ is required. Docker is required for Docker agents, sidecar Gateway mode, Docker verifiers, and Docker Compose benchmark environments.

For Pi support, install Pi separately. Red Harness CI pins Pi 1.0.4:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.0.4
pi --version
```

Pi requires Node.js 22.19 or newer. See [`docs/pi.md`](docs/pi.md) for the full integration contract.

## Local run

```bash
harness run benchmarks/examples/hello/task.yaml \
  --agent agents/examples/demo.yaml \
  --allow-host-agent \
  --seed 42
```

## Pi Agent Adapter

Example `agents/examples/pi.yaml`:

```yaml
apiVersion: harness/v1
id: pi-openai-sol
type: pi

pi:
  provider: openai
  model: gpt-5.6-sol
  thinking: medium
  tools: [read, bash, edit, write]
  approve_project: false
  context_files: false
  extensions: false
  skills: false
  prompt_templates: false
  themes: false
  mcp: false
  offline: true
```

Direct-provider mode uses the provider's normal environment credential:

```bash
export OPENAI_API_KEY="..."

harness run benchmark/task.yaml \
  --agent agents/examples/pi.yaml \
  --allow-host-agent
```

The adapter defaults to Pi's long-lived RPC mode. Harness sends the initial objective and plan
through `prompt`, streams normalized Pi events, and injects verification, world, and benchmark
feedback with `steer` before Pi's next model turn. It waits for `agent_settled`, since `agent_end`
can precede automatic retry or queued work. Set `pi.mode: json` to retain the one-shot compatibility
path.

The normalized event mapping is:

```text
Pi session header (JSON mode) -> pi.session
assistant message_start       -> model.request
assistant message_end         -> model.response + model.usage
tool_execution_start          -> tool.call
tool_execution_end            -> tool.result
agent_settled                 -> pi.agent_settled
```

Pi model usage contributes input/output/total tokens and model cost to the Harness budget and result
metrics. The raw Pi JSONL stream is retained in `agent.stdout.log`.

By default, Pi runs in Docker with a read-only root filesystem, dropped Linux capabilities, bounded CPU/RAM/PIDs, read-only task mount, writable run workspace, and a fresh per-run `PI_CODING_AGENT_DIR`. Pi sessions are ephemeral, telemetry/update checks are disabled, project trust is denied, and project context/extensions/skills/MCP/templates/themes are not loaded unless explicitly enabled.

### Pi through the Harness model Gateway

Pi can be forced through the Harness OpenAI-compatible model proxy:

```bash
export HARNESS_MODEL_API_KEY="real-provider-key"

harness run benchmark/task.yaml \
  --agent agents/examples/pi.yaml \
  --allow-host-agent \
  --gateway \
  --model-upstream https://provider.example/v1 \
  --input-price-per-million 2.0 \
  --output-price-per-million 10.0
```

For that run Harness creates an isolated Pi `models.json` with a `harness` provider whose API key is the one-time `HARNESS_GATEWAY_TOKEN`. The real upstream credential remains inside the Gateway process. Gateway model events are authoritative in this mode, so Pi's copy of model usage is not counted twice.

The Pi adapter is containerized and supports both host Gateway mode and Docker sidecar Gateway mode. For VPN-backed benchmarks such as TSec, `network: host` is available on Linux workers; sidecar Gateway mode is intentionally incompatible with `network: host` because the Agent must join the private Gateway network.

## TSec Benchmark SDK

Install the optional SDK dependency and build the Pi/Kali image:

```bash
python -m pip install -e ".[tsec]"
docker build -t harness/pi-kali:local docker/pi-kali
```

Set `BENCHMARK_BASE_URL` and `BENCHMARK_TOKEN` only on the Harness worker. Run one challenge with:

```bash
harness tsec --agent agents/examples/pi-tsec.yaml
```

Use `--challenge WEB-001` for a specific challenge or `--all` for every unfinished challenge. Harness performs the SDK lifecycle and never passes the Benchmark token into Pi. Pi receives only the challenge description and target addresses and emits candidates as `HARNESS_FLAG=<flag>`. Candidate plaintext is submitted through the SDK; trace records only the SHA256 of each candidate. Challenge close is always executed in `finally`.

For VPN-backed targets, `agents/examples/pi-tsec.yaml` uses `network: host` so the Kali container shares the Linux worker's VPN routes. See [`docs/tsec.md`](docs/tsec.md).

## Audit a saved run

Audit the consistency of a completed run's result, trace, progress, and available
World exports without changing those files:

```bash
harness audit-run .harness/runs/run_...
```

The command prints a JSON report with status `consistent`,
`contradictory`, or `incomplete`; exit codes are 0, 1, and 2 respectively.
Every run writes an append-only `trace.jsonl`; `harness audit-run` checks that the
result, trace, progress, and World exports agree. A consistent report does not
authenticate their sources or independently prove task success.

## Control spine

Red Harness keeps a single closed feedback loop:

```
reference (task / TSec objective) -> error -> controller
   (solver_policy + planner + Pi) -> actuator (tools) -> plant (target)
   -> sensor (verifier / benchmark evaluator) -> estimator (world/) -> error
```

Only the verifier can close the loop: agent claims and tool exit codes are recorded
as claims, not evidence. Open-loop subsystems (control plane, queue, worker,
coordination blackboard, OTLP export, scoring, suite, evaluation, benchmark
registry) were removed to keep the loop minimal. See
[`docs/control-theory.md`](docs/control-theory.md) for the design and the pruning
rationale.

## Gateway modes

### Host mode

Useful for trusted local development and the first-class Pi adapter:

```bash
harness run benchmarks/examples/hello/task.yaml \
  --agent agents/examples/gateway-demo.yaml \
  --allow-host-agent \
  --gateway \
  --gateway-policy examples/gateway-policy.yaml
```

The Agent receives only a one-time Gateway token. Real provider credentials remain on the Harness/Gateway side.

### Isolated Docker sidecar

```bash
docker build -f docker/gateway/Dockerfile \
  -t harness-gateway:local .

harness run benchmarks/examples/hello/task.yaml \
  --agent agents/examples/docker-gateway.yaml \
  --gateway \
  --gateway-mode sidecar \
  --gateway-image harness-gateway:local \
  --gateway-policy examples/gateway-policy.yaml
```

Sidecar mode creates a per-run Docker `--internal` network:

```text
Docker Agent ───── private internal network ───── Gateway sidecar
     │                                               │
     │                                               └─ optional provider egress
     │
     └─ benchmark target network
```

The Gateway publishes no host port. When a provider upstream is configured, only the sidecar is additionally attached to an egress-capable Docker bridge.

## Tool Gateway

The built-in tools remain intentionally narrow:

```text
file.read
file.write
```

The default policy prevents path traversal and constrains reads/writes to task/run roots. No host-side arbitrary shell tool is exposed.

Pi built-in tools do not pass through this Tool Gateway in v0.8; they are observed through Pi's JSON event stream and execute inside the Kali container. The container receives no Docker socket and no host filesystem mounts beyond the benchmark task/run paths.

## Execution profiles

Docker Agent:

```yaml
apiVersion: harness/v1
id: isolated-agent
type: docker
image: my-agent:latest
network: environment
runtime: runsc
```

Docker Verifier:

```yaml
verification:
  type: docker
  image: my-verifier:latest
  command: [python, /task/verifier.py]
  network: none
  runtime: runsc
  timeout: 120
```

Check local support:

```bash
harness capabilities
```

Output reports availability of:

```text
docker
gvisor_runsc
pi
bwrap
```

## Budgets and result bundles

Only the wall-clock deadline bounds a run, and it is **soft**:

```yaml
budgets:
  wall_time: 3600
```

At `wall_time` Harness sends `solver.finalize`, asking the Agent to stop probing and write
`agent.summary.md` (confirmed facts, unconfirmed hypotheses, what was tried and ruled out,
unfinished intents). The run then has a `FINALIZE_GRACE_SECONDS` (180s) window to finish that
report before the container is killed as a last resort.

`max_tokens`, `max_model_calls`, `max_tool_calls` and `max_cost_usd` are still accepted for
backwards compatibility but are **no longer enforced**; declaring one records a `budget.ignored`
trace event and the CLI prints a warning.

Every run also writes `summary.json` and a `summary` block in `result.json`: a deterministic
facts/intents/exploration digest built from World state and progress, plus the Agent's own report.

Each run is stored beneath `.harness/runs/<run_id>/`:

```text
result.json
trace.jsonl
events.jsonl
world.db
world.events.jsonl
world.snapshot.json
world.context.txt
agent.stdout.log
agent.stderr.log
verifier.stdout.log
verifier.stderr.log
```

For Pi runs, `agent.stdout.log` is the raw Pi JSON event stream.

## Architecture

```text
        reference (task / TSec objective)
                 │
                 ▼
        error  ──────────────►  controller
          ▲                  solver_policy + planner + Pi
          │                            │
          │                            ▼
       estimator ◄── sensor ◄── plant ◄── actuator
        world/     verifier    target    Pi tools

        only the sensor (verifier / evaluator) may close the loop
```

World-state design and extension guidance is documented in [`docs/world-state.md`](docs/world-state.md). Reusable technique metadata uses `harness/skill/v1`; skills describe state preconditions and possible outcomes but are not executable code. The core state schema is domain-neutral and does not require a DAG; graph and timeline representations are derived views over the event-sourced state.

Core separation rule:

```text
Task != Environment != Agent != Model != Tool != Verifier
```

The closed-loop control spine, the module that owns each stage, and the pruning
rationale are documented in [`docs/control-theory.md`](docs/control-theory.md).
The agent-side architecture as a whole (adapter/session contracts, solver loop,
rounds, trust tiers, recovery) is consolidated in
[`docs/agent-architecture.md`](docs/agent-architecture.md).

## Roadmap

1. **Done:** task/environment/verifier/result execution contract.
2. **Done:** Docker Agent, budgets, repeat/seed.
3. **Done:** Tool Gateway, model proxy, credential isolation, streaming accounting.
4. **Done:** Docker Verifier sandbox and Docker Agent Gateway support.
5. **Done:** isolated Gateway sidecar, Docker runtime profiles.
6. **Done:** first-class Pi JSON adapter, Pi usage/tool normalization, isolated Pi config, Gateway model routing, Pi capability detection.
7. **Done:** control-spine simplification (open-loop subsystems removed, single feedback loop).
8. **Done:** dead-contract removal and duplicate-path consolidation (Firecracker stub, file world store, unreferenced schemas removed; single Docker command builder, usage accounting and TSec path).
9. Containerized Pi adapter hardening and snapshot pooling.


### Benchmark adapters

External benchmark platforms use a common contract:

```text
discover -> BenchmarkCase
provision -> BenchmarkSession
Agent run
submit -> SubmissionResult
evaluate -> EvaluationResult
teardown
```

A `BenchmarkSession` contains a normalized objective, targets, and benchmark metadata. The generic
`BenchmarkRunner` seeds targets into the World Model, builds `world.context.txt`, runs the selected
Agent adapter, ingests world-state submissions, sends benchmark submissions, records evaluation,
and always tears the external session down.

TSecBench is implemented through this contract. Its SDK token stays in the Harness process and is
never passed to the Agent. `HARNESS_FLAG=<flag>` remains a compatibility submission extractor;
submission values are hashed in normalized traces.


### Solver session runtime

The benchmark runtime can now start Agents through a bidirectional `AgentSession` compatibility
boundary while retaining the legacy `run()` API. Sessions expose normalized events, trusted
observations, checkpoints, close requests, and the eventual `AgentResult`.

Externally evaluated benchmarks may accept structured submissions through
`HARNESS_SUBMISSION_INBOX` and write trusted evaluator feedback to
`HARNESS_FEEDBACK_FILE`. TSec uses this path for online flag feedback while retaining
`HARNESS_FLAG=<flag>` as a compatibility fallback.

Session checkpoints retain event IDs, budget totals, and pending tool actions. Recovery refuses to
continue while an action is unresolved; `harness.runtime.checkpoint.reconcile_pending_actions()`
records an operator-supplied outcome and never reruns the interrupted tool. This provides safe
accounting and reconciliation without claiming exactly-once execution.

Each benchmark run also maintains `progress.json` (`harness/progress/v2`) separately from World
State. It stores Agent-authored facts as claims, distinguishes successful execution from verified
evidence and goal progress, and tracks no-progress/failure counters, budget usage, submissions, and
objective completion. A successful tool exit or accepted submission alone does not establish
objective completion; the final task verifier or benchmark evaluation controls the result.


### Rolling-horizon planning

The Harness exposes an optional short-horizon planner at:

```text
GET /v1/runs/{run_id}/plan/rolling?horizon=1..3
```

It combines the current World Snapshot, Skill Registry, and `progress.json` to return at most three
`PlannedAction` records. Each action includes a skill, expected observations, an inspectable rationale
with goal relevance, information gain, repeated-failure penalty, and remaining-budget fit,
and explicit replan triggers such as action failure, missing expected observations, changed world
revision, repeated no-progress, or contradicted hypotheses.

The planner can filter state requirements by minimum provenance level (`claim`, `evidence`, or
`verified`). Capabilities are checked for expiry. The rolling planner is also used by the interactive
SolverLoop. A session receives an initial
`solver.plan.updated` observation and receives a new plan when verification, repeated no-progress,
or benchmark feedback requests replanning. Plans are persisted to `plan.json` and guide the Agent;
the Harness still does not execute Skill metadata directly as commands. Coordination-plane work
publication remains explicit.


### SQLite World State

SQLite is the default World State backend. `world.db` is authoritative; `world.events.jsonl` and
`world.snapshot.json` are compatibility/debug exports. The database uses WAL mode, persistent event
sequence numbers, event-id deduplication, and optional optimistic `expected_revision` checks.

Incremental consumers can request:

```text
GET /v1/runs/{run_id}/world/events?after_sequence=<sequence>
```

Each returned event includes its persistent `sequence` cursor. Legacy JSONL state can still be
passed to `--resume-world`; new runs may resume directly from a previous `world.db`.
