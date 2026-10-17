# Agent execution boundary

## Assets and trust levels

The run root is Harness-owned state. It contains the authoritative World store and event
history, progress ledger, trace, result, checkpoint, objective evidence, and verifier logs.
Agent output, tool output, submissions, and files in `agent-workspace/` are untrusted.
An Agent claim or successful process exit is never objective evidence by itself.

## Filesystem boundary

Container Agents receive a read-only task input snapshot at `/task` and only the run's
`agent-workspace/` at `/run/harness` read-write. The snapshot omits the configured task verifier,
skips symbolic links, rejects a configured verifier path that contains a symbolic link (including
parent components), is bounded to 4,096 files and 2 GiB, and is checked against its source on
resume. Preflight and snapshot creation both enforce the verifier path check before allocating the
Agent task view. They do not receive a mount of the run root,
World database, result, checkpoint, trace, evidence store, or verifier logs. The workspace
contains writable artifacts and untrusted event/submission input, plus refreshed copies of
`world.context.txt` and `progress.json`; those copies are for Agent guidance and are not read
back as authoritative state. The verifier runs from the original Harness-side task directory;
its source is excluded from the Agent's task input snapshot, and it sees the workspace read-only.

Harness reads Agent-produced files as hostile input: JSONL inboxes and telemetry require
regular files opened without following a final symlink. Gateway writes use the same rule.
The task mount and container root filesystem are read-only; only `/tmp` and the Agent
workspace are writable. No Docker socket is mounted.

### Competing sessions

When an Agent declares `executors`, each session keeps its own container and its own
`agent-workspace`, `notes`, progress ledger and checkpoint path, so one session can neither read
nor overwrite another session's control state. The single deliberate exception is
`executor-shared/`, mounted at `/run/harness-shared` read-write in every session of that case and
holding only the Agent-authored `shared-findings.md` plus Harness-written archive snapshots.
Nothing authoritative may live there: it must never contain `world.db`, `world.events.jsonl`,
`progress.json`, `checkpoint.json`, `result.json`, evidence or verifier logs, and
`tests/test_executors.py` asserts that boundary. Findings text is untrusted Agent output and is
read back only for archival, never as evidence or instruction. The run root itself stays
unmounted from every session container.

### Operator hint inbox

`harness hint <run_dir>` appends to `hints.jsonl` at the run root, which no session container
can reach, so a hint can only be written by an operator on the host. That mount boundary is what
lets the SolverLoop record each hint as an evidence-tier `human.hint` World observation and steer
it into the running Agent: hints redirect exploration, they are never a verdict, and only a
verifier or benchmark evaluation can close the objective. If the run root ever became
container-writable, the evidence-tier status of `human.hint` would have to be revisited, because
an Agent-authored copy would then be forged operator input rather than a verified fact.

### Opt-in Harness Pi extension

With `pi.harness_extension: true`, the Harness writes its own TypeScript extension to
`run_dir/pi-agent/extensions/harness-context.ts` (visible in the container as
`/run/harness/pi-agent/extensions/harness-context.ts`) and stops passing `--no-extensions`, so
Pi loads exactly that Harness-authored file at startup. The extension runs with Agent
privileges and has exactly three handlers: a `tool_result` spill that writes oversized output
into the Agent workspace while keeping a bounded preview in context, a
`session_before_compact` summary carrying the Harness retention checklist, and a `tool_call`
clamp that lowers the requested bash timeout to 180s. The clamp only narrows what the Agent
asks for: it grants nothing, and the Harness-side `tool.slow` event records every call that
still runs long. It adds no network
capability, cannot sign verdicts, never reads credentials or the run root, and everything it
writes is Agent-side output, not evidence. The default is off; when off, behaviour is
unchanged. The competition profiles (`agents/examples/pi-tsec-opencode.yaml`,
`agents/examples/zc-offensive.yaml`) opt in: the 2026-10-16 sweep lost 19 tool calls to
180s-plus waits and one command held 927s of an 1800s window. Every other config keeps the
default. Other extension sources stay disabled unless `pi.extensions: true` is set
explicitly, which no config does.

## Process and network boundary

Docker Agents run as **root inside the container** (decision recorded in
[roadmap/execution.md](../roadmap/execution.md) EXE-09), use a read-only root filesystem, drop
all capabilities by default and set `no-new-privileges`, so the container root has no power the
dropped set does not restore. Two capabilities are the container baseline
(`ROOT_AGENT_CAPABILITIES` in `agent.py`): `CAP_DAC_OVERRIDE` and `CAP_FOWNER`, because a
cap-dropped root is *weaker* than the workspace owner and could otherwise not write the
harness-owned `agent-workspace/` at all. Because the override makes permission bits moot for the
Agent, the workspace stays owned by the harness identity with owner-only modes, and the non-root
Harness keeps the write access its atomic view replacement and artifact harvest need. The
override reaches only what is mounted: the workspace and tmpfs are writable, the task snapshot
and knowledge roots stay read-only mounts, and the run root, World store, result, checkpoint,
trace, evidence and verifier logs remain unmounted. Declared capabilities such as `NET_RAW` are
added on top and are the only way to reach raw sockets; without a declared capability, port
scanning must use connect-based tools (`nmap -sT`, `nc`).
Container networking is selected from
the Agent's declared network profile and network mode. `fully-offline` (and legacy `offline`)
uses Docker `none`. `target-only` requires the target Compose network to report Docker
`Internal=true` and attaches the Agent only to that network. `model-allowed` requires the
sidecar Gateway network and any configured target network to report `Internal=true`; the Agent
uses the Docker network IDs returned by those checks while the Gateway proxies the configured
model endpoint. Using the checked IDs for container creation and target-network attachment
prevents a network name from being rebound to a different network between validation and use.
These strict profiles are checked before Agent startup. Strict-profile Agent containers explicitly
clear proxy variables, including values Docker CLI can inject from its client configuration, and reject
Agent-configured proxy passthrough. This prevents proxy credentials from entering the Agent
environment and prevents direct proxy egress from bypassing the Gateway. Legacy
`benchmark-only` remains advisory and does not provide egress isolation. Host CLI Agents cannot
use strict profiles.
Host networking is a deliberate configuration with broader reach and is incompatible with
sidecar Gateway mode.

Python task verifiers run under bubblewrap with a private network namespace, no Linux
capabilities, read-only task/workspace views, a minimal environment, and resource limits.
Unavailable isolation fails closed. Host CLI Agents require explicit opt-in and are not a
container security boundary.

## Secrets and artifacts

Benchmark credentials remain in Harness adapters and are not copied into Agent containers.
Provider credentials are passed only when the Agent configuration explicitly requests
environment passthrough; container Agent configs cannot passthrough token-named variables or
reserved Harness, benchmark, and TSec credentials. Gateway mode injects a per-run Gateway token
instead of the direct provider key. Agent output remains untrusted even when stored as a run
artifact. Objective verdicts are produced by registered verifiers and reference Harness-persisted
evidence.

Secrets the engagement itself discovers stay out of the derived fact graph in plaintext:
`harness.extract` records a `credential.access` fact with the service, host, port, login and a
truncated digest of the observed secret, never the secret itself, because those facts are
rendered into the Agent's context file and exported through the run summary. The raw tool output
stays where the Agent already has it: its own workspace and event stream.

Host CLI Agents are an explicit development escape hatch: they are not isolated and inherit
the host process environment. Run only trusted commands in that mode and keep sensitive
benchmark credentials out of their environment.

This boundary limits filesystem access; it does not claim exactly-once side effects, safe
host networking, or trust in Agent-generated files. Interrupted external actions require
reconciliation before recovery.
