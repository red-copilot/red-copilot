# EXE — Agents, tools, containers and gateway

**Scope:** `agent.py`, `session.py`, `pi_adapter.py`, `pi_container.py`, `gateway.py`, `gateway_runtime.py`, `tool_adapter.py`, `environment.py`, `docker/pi-kali/`. Pi runs inside Kali Rolling containers with long-lived RPC where configured.

### EXE-01 [x] P0 — Execution trust boundary
Deliver: documented threat model plus enforceable mounts, secret isolation, rootfs permissions, capability/PID/CPU/RAM limits, network rules, a non-privileged default and artifact handoff; the Agent must not reach the authoritative World DB, verifier source or credentials.
Accept: integration tests attempt to mutate protected state, obtain secrets, escape the allowed workspace and contact denied destinations; failures are observable.
State: per-run `agent-workspace/` separation, numeric UID/GID ownership, no-follow file access, a verifier-free bounded task snapshot, blanked Docker proxy defaults, explicit `cap_add` opt-in, shared `build_container_command` limits and six enforced network profiles are implemented. The isolation-probe boundary test now runs in CI (`tests/test_container_boundary.py`, executed after the probe image build) and passed locally against `harness-isolation-probe:ci`.

### EXE-02 [~] P1 — Adapter/session contract and budgets
Deliver: normalized event IDs, settled/terminal semantics, tool-call correlation and per-run token/tool/wall-clock/cost accounting without double charging through both Gateway and Pi.
Accept: fake Pi and real Docker smoke tests covering malformed RPC streams, settle/retry, cancellation, model errors, Gateway disconnect and budget exhaustion.
State: strict typed adapters (`AgentRunContext`, `SessionPhaseTracker`, `AgentCapabilities`), trusted Gateway-side usage accounting, fail-closed telemetry validation and multi-dimension soft budgets riding the wall-clock finalize path with `FINALIZE_GRACE_SECONDS` are implemented. Outstanding: live Docker Pi/Gateway smoke and the live container budget smoke.

### EXE-03 [~] P1 — Offline profiles and deterministic build
Deliver: separate `target-only`, `model-allowed` and `fully-offline` networking, pinned image dependencies and pre-installed tools with no package installation while solving.
Accept: a no-public-egress smoke suite in a clean runner plus network policy tests.
State: profiles are enforced from inspected internal-network IDs, preflight describes the required topology, and the Pi image pins the Kali OCI digest, rewrites `kali-rolling` to `kali-last-snapshot`, pins the Pi version and ships wordlists. Internal-target egress denial now runs in CI (`tests/test_network_profiles_docker.py`) and passed locally; clean-runner image reproducibility evidence remains outstanding.

### EXE-04 [x] P2 — Tool execution capabilities
Deliver: domain-agnostic `ToolAdapter` with structured metadata, cancellation and evidence references, explicit registration only, and preserved approval/authorization boundaries.
Accept: contract tests across filesystem, local process and authorized network tools; outputs remain untrusted.
State: implemented in `tool_adapter.py` with Gateway allowlisting, fail-closed deduplication and untrusted content-hash evidence. The optional sandboxed process and CIDR-restricted HTTP plugins were removed on 2026-10-15 as unused production code; the contract and Gateway authorization remain.

### EXE-05 [~] P1 — Agent session contract consolidation
Deliver: one typed `AgentRunContext`, an observable session phase machine and declared `AgentCapabilities` with traced negotiation so one-shot degradation is contract rather than accident.
Accept: all adapter call sites use the context object; phase transitions are validated and mirrored into the trace; capability emission appears in local and benchmark traces.
State: implemented in `agent.py` and `session.py`; live Docker/RPC smoke remains before acceptance.

### EXE-06 [~] P2 — Single-source typed event schemas
Deliver: one pydantic schema per Agent-authored event type, validated at the ACI boundary so wire contract, tests and docs cannot drift.
Accept: the registry covers exactly the typed events `TypedACI` translates; malformed payloads fail closed into `aci.feedback` without state changes.
State: implemented in `agent_events.py::TYPED_EVENT_SCHEMAS` with a documented fail-closed tightening on known keys with wrong types; a live Pi run remains before acceptance.

### EXE-07 [ ] P1 — Tool availability over tool count
Deliver: fix or explicitly retire the tools the Agent actually reached for and could not use -
nmap is broken in-run (`/usr/lib/nmap/nmap: Operation not permitted`, 28 occurrences) - and
pre-install the missing basics (`unzip`, `xxd`, `php-cli`, `iptables`, `traceroute`, the Python
`requests`/`bs4`/`lxml`/`PIL`/`pycryptodome`/`paramiko` stack) in the offline image; the prompt
carries the documented replacement path for anything retired.
Accept: the image static contract test asserts the added tools and that nothing installs at solve
time; an in-container probe runs the replacement reconnaissance path and imports the Python
stack; the image size delta is recorded.
State: pending. Evidence: 28 `Operation not permitted` results from nmap, `unzip`/`xxd`/`php`
missing in session transcripts, `pip install` unusable under the offline profile, and zero calls
to five of the ten Kali tools the image already ships. Capability or seccomp changes need
maintainer review before the code moves.

### EXE-08 [ ] P2 — Rendered observation (browser), phased
Deliver: phase one is a Harness-side read-only `render(url)` observation (status, DOM text,
redirect chain, XHR list) recorded to the trace and usable as a VER-05 observation source, with
chromium and playwright baked into the offline image; phase two - letting the Agent drive a
browser freely - waits on maintainer review because `cap-drop ALL` with no-new-privileges
requires `--no-sandbox`, which is an isolation downgrade.
Accept: phase one is covered offline against a local fixture page plus the image static contract
and a recorded size delta; phase two has no acceptance path until the boundary review concludes.
State: pending, deliberately unproven: c-01 spent 136 tool calls and 70 model calls on a Vue SPA
and scored zero, but the seven unsolved `a-*` runs stalled on finding a bypass predicate rather
than on rendering, so this item is a bet rather than a measured bottleneck.

**Non-goals:** arbitrary unsandboxed host execution as a default, dynamic tool downloads in offline competition runs, schema-ing the Harness-normalized `tool.*`/`model.*` streams, multi-agent session contracts.

