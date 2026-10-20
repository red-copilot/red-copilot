# EXE — Agents, tools, containers and gateway

**Scope:** `agent.py`, `session.py`, `pi_adapter.py`, `pi_container.py`, `subagent.py`, `gateway.py`, `gateway_runtime.py`, `tool_adapter.py`, `environment.py`, `docker/pi-kali/`. Pi RPC inside a Kali Rolling container is the only Agent execution path; the Gateway is host-only.

### EXE-01 [x] P0 — Execution trust boundary
Deliver: documented threat model plus enforceable mounts, secret isolation, rootfs permissions, capability/PID/CPU/RAM limits, network rules, a non-privileged default and artifact handoff; the Agent must not reach the authoritative World DB, verifier source or credentials.
Accept: integration tests attempt to mutate protected state, obtain secrets, escape the allowed workspace and contact denied destinations; failures are observable.
State: per-run `agent-workspace/` separation, no-follow file access, a verifier-free bounded task snapshot, blanked Docker proxy defaults, a cap-dropped container with an explicit capability baseline plus `cap_add` opt-in, shared `build_container_command` limits and six enforced network profiles are implemented. The isolation-probe boundary test runs in CI (`tests/test_container_boundary.py`, executed after the probe image build) and passed locally against `harness-isolation-probe:ci`; since EXE-09 it asserts the mount, capability and environment boundary instead of a non-root container UID.

### EXE-02 [~] P1 — Adapter/session contract and budgets
Deliver: normalized event IDs, settled/terminal semantics, tool-call correlation and per-run token/tool/wall-clock/cost accounting without double charging through both Gateway and Pi.
Accept: fake Pi and real Docker smoke tests covering malformed RPC streams, settle/retry, cancellation, model errors, Gateway disconnect and budget exhaustion.
State: strict typed adapters (`AgentRunContext`, `SessionPhaseTracker`, `AgentCapabilities`), trusted Gateway-side usage accounting, fail-closed telemetry validation and multi-dimension soft budgets riding the wall-clock finalize path with `FINALIZE_GRACE_SECONDS` are implemented. Outstanding: live Docker Pi/Gateway smoke and the live container budget smoke.

### EXE-03 [~] P1 — Offline profiles and deterministic build
Deliver: separate `target-only` and `fully-offline` networking, pinned image dependencies and pre-installed tools with no package installation while solving.
Accept: a no-public-egress smoke suite in a clean runner plus network policy tests.
State: profiles are enforced from inspected internal-network IDs, preflight describes the required topology, and the Pi image pins the Kali OCI digest, rewrites `kali-rolling` to `kali-last-snapshot`, pins the Pi version and ships wordlists. Internal-target egress denial now runs in CI (`tests/test_network_profiles_docker.py`) and passed locally; clean-runner image reproducibility evidence remains outstanding.

### EXE-04 [x] P2 — Tool execution capabilities
Deliver: domain-agnostic `ToolAdapter` with structured metadata, cancellation and evidence references, explicit registration only, and preserved approval/authorization boundaries.
Accept: contract tests across filesystem, local process and authorized network tools; outputs remain untrusted.
State: implemented in `tool_adapter.py` with Gateway allowlisting, fail-closed deduplication and untrusted content-hash evidence. The optional sandboxed process and CIDR-restricted HTTP plugins were removed on 2026-10-15 as unused production code; the contract and Gateway authorization remain.

### EXE-05 [~] P1 — Agent session contract consolidation
Deliver: one typed `AgentRunContext`, an observable Pi RPC session phase machine and the single live-steer capability record mirrored into the trace.
Accept: all adapter call sites use the context object; phase transitions are validated and mirrored into the trace; capability emission appears in local and benchmark traces.
State: implemented in `agent.py` and `session.py`; live Docker/RPC smoke remains before acceptance.

### EXE-06 [~] P2 — Single-source typed event schemas
Deliver: one pydantic schema per Agent-authored event type, validated at the ACI boundary so wire contract, tests and docs cannot drift.
Accept: the registry covers exactly the typed events `TypedACI` translates; malformed payloads fail closed into `aci.feedback` without state changes.
State: implemented in `agent_events.py::TYPED_EVENT_SCHEMAS` with a documented fail-closed tightening on known keys with wrong types; a live Pi run remains before acceptance.

### EXE-07 [x] P1 — Tool availability over tool count
Deliver: fix or explicitly retire the tools the Agent actually reached for and could not use -
nmap is broken in-run (`/usr/lib/nmap/nmap: Operation not permitted`, 28 occurrences) - and
pre-install the missing basics (`unzip`, `xxd`, `php-cli`, `iptables`, `traceroute`, the Python
`requests`/`bs4`/`lxml`/`PIL`/`pycryptodome`/`paramiko` stack) in the offline image; the prompt
carries the documented replacement path for anything retired.
Accept: the image static contract test asserts the added tools and that nothing installs at solve
time; an in-container probe runs the replacement reconnaissance path and imports the Python
stack; the image size delta is recorded.
State: implemented. Evidence: 28 `Operation not permitted` results from nmap, `unzip`/`xxd`/`php`
missing in session transcripts, `pip install` unusable under the offline profile, and zero calls
to five of the ten Kali tools the image already ships. The image adds `unzip`, `xxd`, `php-cli`,
`traceroute`, `iptables` and the Python stack, and strips nmap's file capabilities (see EXE-09 for
why the exec failed even before privileges; no capability or seccomp relaxation was needed).
Measured, not estimated: `docker image inspect` reports 2,868,832,915 bytes in the 2026-10-14
baseline against 2,983,477,199 bytes now (+114,644,284 bytes, +0.11 GiB), while the file a
platform actually receives is much smaller - `docker save ... | wc -c` reports 766,589,952 bytes
and the gzipped form 763,061,574 bytes, both inside the 3 GB upload ceiling. Verified by `tests/test_pi_kali_image.py` (static contract: added tools,
the capability strip, and no solve-time install anywhere in the image or the agent-facing
markdown) and `tests/test_pi_kali_tools_docker.py` (connect and raw-socket scans, the Python
stack, and a root-written artifact the harness can read back). The prompt and
`skills/offensive/active-recon` now carry the connect-scan default and the fallback path.

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

### EXE-09 [x] P0 — Root container identity for raw-socket tooling
Deliver: the Agent container runs as root inside a read-only, capability-dropped container so
declared capabilities such as `NET_RAW` actually apply, with the workspace kept harness-owned and
owner-only and the changed isolation contract documented before the code moved.
Accept: the container command pins the root identity and the baseline capability set; the CI
isolation probe still proves the Agent cannot read or forge authoritative state, escape the
writable mount or reach the denied network; the workspace stays readable by a non-root harness.
State: implemented (maintainer-reviewed boundary change, decided on the 2026-10-16 sweep data).
Measured on the sweep's Kali image under the exact container settings (non-root user, capabilities
dropped, `no-new-privileges`): the declared `cap_add: [NET_RAW]` was inert (`CapEff:
0000000000000000`) and `/usr/bin/nmap: exec: /usr/lib/nmap/nmap: Operation not permitted` matched
the 28 in-run failures. Under the root identity the same probe reports `CapEff:
0000000000002000` and `nmap -sS` succeeds, while the file-capped nmap still fails under
`no-new-privileges`, so the image keeps `setcap -r`. A cap-dropped root is also weaker than the
workspace owner: the baseline `ROOT_AGENT_CAPABILITIES` (`CAP_DAC_OVERRIDE`, `CAP_FOWNER`) is
what lets the Agent write the owned-by-harness `0700` workspace, measured by writing into one. The
workspace stays owner-only, and the mounts, the network profiles and the un-mounted run root are
unchanged. Evidence: `tests/test_agent_workspace.py`, `tests/test_container_boundary.py`,
`tests/test_pi_adapter.py`, `tests/test_runtime_profiles.py`.

**Non-goals:** arbitrary unsandboxed host execution as a default, dynamic tool downloads in offline competition runs, schema-ing the Harness-normalized `tool.*`/`model.*` streams, multi-agent session contracts.

