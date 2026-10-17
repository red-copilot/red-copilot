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
[agent-architecture.md](agent-architecture.md).

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

Optional limits, knowledge roots and parallelism:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --all \
  --knowledge-root skills/offensive \
  --parallel 3 \
  --wall-time 3600 \
  --start-retries 2 \
  --retry-delay 2
```

`--parallel N` solves up to N challenges at once (default 1, hard cap 3 because the platform
allows each team three running instances). Each challenge runs in its own run directory with
its own budgets, so one slow challenge cannot starve the others.

## Sweep behaviour

Before selecting cases, `harness tsec` runs a bounded platform preflight (VPN health check plus
the challenge listing, 45s total). A platform-wide failure exits with code 3, prints the
classified kind (`vpn_unreachable`, `platform_unreachable`, `instance_conflict`) and creates no
run directories; do not retry in a shell loop, fix the platform instead. Exit code 4 means the
sweep ran but some cases produced no run, and the per-case detail is in the summary artifact.

Every sweep writes `sweep-summary-<UTC>.json` under the runs root (`--summary-out` overrides the
path) with the selected cases, per-case status, score, submissions attempted, duration and
tallies, plus the skipped cases and the classified failures. Run directories are created only
after the challenge is provisioned, so a refused start leaves nothing behind;
`harness gc-runs --root .harness/tsec-runs` reports (and with `--apply` deletes) directories
that hold no artifact at all.

`--resume` (default) skips cases that already have a completed local result and seeds a retried
case with the previous attempt's `agent.summary.md`; `--rerun` ignores local history, and an
explicitly named `--challenge` is always run. One case failing to start is recorded and the
remaining cases still run; a platform-wide failure stops new dispatches while the runs already in
flight keep their artifacts.

`--wall-time` is a soft deadline: at that point Harness steers the Agent to write
`agent.summary.md` (facts, ruled-out hypotheses, unfinished intents) and grants a
`FINALIZE_GRACE_SECONDS` (180s) window before the container is killed. The
`--max-tokens`/`--max-model-calls`/`--max-tool-calls`/`--max-cost-usd` flags are enforced the
same soft way: the first crossing records a `budget.exceeded` trace event, steers finalize
(`solver.finalize`, `reason=soft_budget_limit`) and only ends the run if the grace window
elapses without the summary.

Because a TSec run is scored only through submitted candidates, the finalize steer also asks the
Agent to append its best current candidate to `$HARNESS_SUBMISSION_INBOX` before writing the
report, and a benchmark run that reaches half its wall clock without a single submission is
steered once (`solver.submission_due`) to consolidate or change attack surface. World updates are
written to the context projection without an extra steer; only authoritative benchmark feedback,
operator hints and the bounded submission/finalize closure reach the live session.
If the Agent settles without submitting, `solver.submission_reask` asks once for the best
candidate already in its notes; if the process has ended and at least 60 seconds remain,
`BenchmarkRunner` starts one closing session that is restricted to that submission or a final
summary. Both paths are counted in the `submission` steer bucket; no submission attempt means
no new probing turn.

When a TSec challenge is provisioned, `bootstrap_solver()` seeds each `benchmark.target` entity alongside an `evidence`-level `network.reachability` capability scoped to the target address, so the Agent's world context starts from a verified reachability fact. The Agent decides what to do with it; the Harness publishes no plan.

If the platform returns `ResourceUnavailable` while starting a challenge, Harness retries according to `--start-retries` and `--retry-delay`. Other SDK state errors are recorded in the trace and surfaced instead of being guessed around.

Hints are opt-in because the TSec SDK documents that viewing a hint reduces flag score:

```bash
harness tsec \
  --agent agents/examples/pi-tsec-hackathon.yaml \
  --challenge WEB-001 \
  --hint
```

Harness calls `get_hint()` only when `--hint` is present and records that a score penalty is expected.

## ZC offensive agent

`agents/examples/zc-offensive.yaml` declares read-only `knowledge:` roots
(`skills/offensive`, `knowledge`), mounted at `/knowledge/<name>` and exported as
`HARNESS_KNOWLEDGE_DIRS`. The Agent reads the `SKILL.md` catalogue and staged payload notes on
demand with its own file tools; nothing in the Harness parses or ranks them.

```bash
harness tsec --agent agents/examples/zc-offensive.yaml --all --parallel 3 \
  --knowledge-root skills/offensive --wall-time 3600
```

CVM deployment checklist: [agent-architecture-v2.md](agent-architecture-v2.md).

### Competing sessions on one challenge

A `pi` agent may declare several competing sessions for the same challenge:

```yaml
executors:
  - id: primary
    model: gpt-5.6-sol
    thinking: high
  - id: challenger
    model: gpt-5.6-sol
    thinking: medium
```

`--parallel` still decides how many challenges run at once; every declared executor adds one
container to that challenge, so keep `parallel x executors` inside the machine budget. All
sessions share the provisioned instance, the World, the submission dedupe set and the final
evaluation; the first session whose submission the evaluator confirms stops the others and
releases the instance once. Each session keeps its own container, workspace, notes, progress
ledger and `checkpoint.json` under `run_dir/executors/<nn>-<id>/`, splits the token / model-call /
tool-call / cost budgets evenly, and shares the wall-clock window. Findings a session wants its
siblings to see go to `$HARNESS_SHARED_FINDINGS`; `result.json` reports every session under
`executor_sessions`. The local `harness run` entry point still rejects competing executors.

## 接入面核对清单（ZCP-01）

离线部分已就绪：SDK 可用性、base URL 与 token env 由 `harness preflight --tsec` 诊断，
env 名可用 `--base-url-env` / `--benchmark-token-env` 覆盖；客户端契约由
`tests/test_tsec_adapter.py` 固定。**实时核对未执行**（需要平台窗口与真实实例操作），
下表在联调日逐项填写并留档：

| 调用 | 客户端方法 | 待核实 | 需记录的证据 |
| --- | --- | --- | --- |
| 题目列表 | `list_challenges()` | 实际端点、方法、认证头名 | 端点、认证头、响应字段（unique_code/flag_count/score） |
| 启动实例 | `start_challenge(code)` | 与 SDK `openapi/v1` 的差异 | 端点、请求体、`container_addr` 字段位置 |
| 提交 flag | `submit_flag(code, flag)` | 认证头与重复提交语义 | 端点、幂等响应、计分字段 |
| 获取提示 | `get_hint(code)` | 只对已启动实例有效与否 | 端点、扣分说明、响应字段 |
| 关闭实例 | `close_challenge(code)` | 幂等性与释放语义 | 端点、响应、实例对账结果 |

证据不足时保持"离线就绪、实时 blocked"，不得用离线绿测代替实时核对结论。

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
    "hint_used": true
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
