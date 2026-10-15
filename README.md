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
- agent-native cognition: the Agent owns the reasoning loop and keeps its own
  markdown notes, while the Harness only exposes read-only knowledge roots
  (`AgentSpec.knowledge`) and a fact projection; see
  [docs/agent-engineering.md](docs/agent-engineering.md)
- competing solver sessions per provisioned case (`AgentSpec.executors`) with
  per-session workspaces, shared submission dedupe and one authoritative result;
  see [docs/agent-engineering.md](docs/agent-engineering.md)
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
   (solver_policy + Pi) -> actuator (tools) -> plant (target)
   -> sensor (verifier / benchmark evaluator) -> estimator (world/) -> error
```

Only the verifier can close the loop: agent claims and tool exit codes are recorded
as claims, not evidence. Open-loop subsystems (control plane, queue, worker,
coordination blackboard, OTLP export, scoring, suite, evaluation, benchmark
registry) were removed to keep the loop minimal. See
[`docs/agent-architecture-v2.md`](docs/agent-architecture-v2.md) for the design and the
pruning rationale, and [`docs/agent-engineering.md`](docs/agent-engineering.md) for the
agent-side engineering plan.

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

Every declared budget dimension is enforced as a **soft** ceiling:

```yaml
budgets:
  wall_time: 3600
  max_tokens: 2000000
  max_model_calls: 400
  max_tool_calls: 800
  max_cost_usd: 25
```

Only the dimensions you declare are bounded; an undeclared one imposes no limit. Crossing any
of them — the wall-clock deadline or a usage ceiling reported in the Agent's telemetry — emits
a `budget.exceeded` trace event (carrying `budget`, `limit`, `observed` and `grace_seconds`)
and sends `solver.finalize`, asking the Agent to stop probing and write `agent.summary.md`
(confirmed facts, unconfirmed hypotheses, what was tried and ruled out, unfinished intents).
The run then has a `FINALIZE_GRACE_SECONDS` (180s) window to finish that report before the
process or container is killed as a last resort.

Usage ceilings are computed from Agent-reported telemetry, so they decide when to wind the run
down rather than what it finally cost; malformed or missing usage fails closed and stops it.

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
          ▲                  solver_policy + Pi
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
rationale are documented in [`docs/agent-architecture.md`](docs/agent-architecture.md)
together with the agent-side architecture as a whole (adapter/session contracts,
solver loop, rounds, trust tiers, recovery).

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
10. **Done:** repository slimming (production-unreachable modules and orphaned fixtures removed, stale design docs merged into [`docs/agent-architecture.md`](docs/agent-architecture.md), roadmap trimmed to item definitions, redundant tests cut).


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

Each benchmark run also maintains `progress.json` (`harness/progress/v4`) separately from World
State. It stores Agent-authored facts as claims, distinguishes successful execution from verified
evidence and goal progress, and tracks no-progress/failure counters, budget usage, submissions, and
objective completion. A successful tool exit or accepted submission alone does not establish
objective completion; the final task verifier or benchmark evaluation controls the result.


### Agent cognition and competing sessions

The Harness no longer plans, ranks skills, or compiles rounds. Cognition lives in the Agent:
it keeps its own markdown notes under `$HARNESS_NOTES_DIR`, reads read-only knowledge roots
(`AgentSpec.knowledge`, mounted at `/knowledge/<name>`) on demand, and treats the projected
`world.context.txt` as observations rather than instructions. The Harness keeps the environment,
trust, accounting, recovery and the bounded stop law (`solver_policy.decide_solver_action`).

A case may declare several competing solver sessions (`AgentSpec.executors`, at most four). Each
session gets its own container, workspace, notes, progress ledger and checkpoint under
`run_dir/executors/<nn>-<id>/`; they share one provision, one World, one submission dedupe set and
one authoritative result, and the first session whose submission the final evaluator confirms
stops the rest. Usage budgets are split evenly across sessions while the wall-clock window stays
whole. Findings a session wants to share with its siblings go to `$HARNESS_SHARED_FINDINGS`, the
only cross-session writable path. See [docs/agent-engineering.md](docs/agent-engineering.md).


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
