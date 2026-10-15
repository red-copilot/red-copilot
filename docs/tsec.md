# TSec Benchmark SDK integration

Red Harness owns the TSec control-plane credential and challenge lifecycle. The Pi Agent runs inside the Kali container and receives only the challenge description and target addresses.

## Install

```bash
python -m pip install -e ".[tsec]"
docker build -t harness/pi-kali:local docker/pi-kali
```

Configure the platform SDK:

```bash
export BENCHMARK_BASE_URL="https://benchmark.example.com"
export BENCHMARK_TOKEN="..."
export OPENAI_API_KEY="..."
```

The worker must already have the Benchmark VPN connected.

## Agent

```yaml
apiVersion: harness/v1
id: pi-kali-tsec
type: pi
image: harness/pi-kali:local
network: host
pi:
  provider: openai
  model: gpt-5.6-sol
  tools: [read, bash, edit, write]
  env_passthrough: [OPENAI_API_KEY]
  cap_add: [NET_RAW]
```

On Linux, `network: host` is the recommended TSec mode because the Kali container directly shares the Worker network namespace and VPN routes. If your VPN permits Docker bridge forwarding, `network: environment` can be used instead.

For the benchmark run, `agents/examples/pi-tsec-hackathon.yaml` adds a
control-loop system prompt (`pi.system_prompt_file: prompts/tsec-solver.md`). The
prompt tells Pi to observe/act/verify/adjust, to treat Harness verification as the
only authoritative feedback, and to submit candidates via `$HARNESS_SUBMISSION_INBOX`.
The ablation matrix (Pi-only vs feedback vs full control spine) is in
[control-theory.md](control-theory.md).

## Run

One unfinished challenge:

```bash
harness tsec --agent agents/examples/pi-tsec-hackathon.yaml
```

Specific challenge:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --challenge WEB-001
```

All unfinished challenges:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --all
```

Optional limits and ablation profiles:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --all \
  --solver-profile pi-world-heuristic \
  --skills-root skills \
  --max-unconfirmed-progress 160 \
  --max-replans 16 \
  --start-retries 2 \
  --retry-delay 2
```

`--wall-time` (default: the managed evaluation platform's eight-hour task window)
is a soft deadline: at that point Harness steers the Agent to write
`agent.summary.md` (facts, ruled-out hypotheses, unfinished intents) and grants a
`FINALIZE_GRACE_SECONDS` (180s) window before the container is killed. The
`--max-tokens`/`--max-model-calls`/`--max-tool-calls`/`--max-cost-usd` flags are still accepted
but no longer enforced; passing one prints a warning and records a `budget.ignored` trace event.

When a TSec challenge is provisioned, `bootstrap_solver()` seeds each `benchmark.target` entity alongside an `evidence`-level `network.reachability` capability scoped to the target address so `RollingHorizonPlanner` immediately produces an initial 3-step plan (`plan.json`) across bundled multi-domain skills (`network`, `web`, `pwn`, `crypto`, `cloud`, `benchmark`).

If the platform returns `ResourceUnavailable` while starting a challenge, Harness retries according to `--start-retries` and `--retry-delay`. Other SDK state errors are recorded in the trace and surfaced instead of being guessed around.

Hints are opt-in because the TSec SDK documents that viewing a hint reduces flag score:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --challenge WEB-001 \
  --hint
```

Harness calls `get_hint()` only when `--hint` is present and records that a score penalty is expected.

## Security boundary

The Pi container never receives `BENCHMARK_TOKEN` or `BENCHMARK_BASE_URL`. Red Harness calls the SDK to start, submit and close challenges. Pi emits candidate answers to `$HARNESS_SUBMISSION_INBOX` (or compatibility fallback `HARNESS_FLAG=flag{...}`).

Harness extracts them and submits them through the SDK. Trace records a SHA256 hash of candidate flags, not plaintext flag values.

Challenge close is executed in `finally`, so target resources are released even when Pi or submission fails.


## Progress and score semantics

Harness preserves the SDK's distinction between challenge score and platform cumulative score.

A TSec result records:

```json
{
  "score": 35,
  "platform_cumulative_score": 935,
  "benchmark": {
    "challenge_total_score": 120,
    "hint_used": true,
    "solver_profile": "pi-world-heuristic"
  },
  "flags": {
    "expected": 2,
    "initial_correct": 1,
    "remaining_at_start": 1,
    "correct": 2
  }
}
```

`score` is the sum of `SubmitResult.awarded` values produced by this Harness run. `platform_cumulative_score` is the SDK's `cumulative_score` and is kept separate because it is cumulative platform progress, not necessarily the score earned by the current challenge attempt.

If a challenge was partially solved before the run, Pi is told only how many flags remain. `DuplicateSubmit` is treated as an idempotent submission and does not expose the plaintext flag in trace output.

The SDK context manager performs the VPN preflight before challenge API calls. Harness converts a `VpnCheckError` into an actionable TSec adapter error while preserving the SDK-provided detail reason.
