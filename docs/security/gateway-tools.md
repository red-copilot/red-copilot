# Gateway tool adapters

Gateway adapters are opt-in and are injected through the domain-neutral
`ToolAdapter` protocol in `harness/tool_adapter.py`. Supplying an adapter does
not authorize it: the Gateway policy must also list its tool name in
`allowed_tools`. The default policy exposes only `file.read` and `file.write`.

```python
from harness.gateway import create_gateway_app
from harness.policy import GatewayPolicy

app = create_gateway_app(
    event_file=run_dir / "events.jsonl",
    workspace=workspace,
    task_dir=agent_visible_task_dir,
    gateway_token=token,
    policy=GatewayPolicy(allowed_tools={"file.read", "file.write"}),
    tool_adapters=(my_adapter,),
)
```

Concrete process and network adapters are deliberately not shipped: the
optional `SandboxedProcessToolAdapter` and `AuthorizedHTTPToolAdapter` were
removed on 2026-10-15 as unused production code. Reinstate an adapter only
behind maintainer review, because authorizing a tool name authorizes whatever
that adapter does inside the Agent-visible boundary. Adapter results keep the
contract guarantees: bounded JSON output, execution status, duration, sanitized
metadata, immutable content-hash references explicitly marked untrusted, 409 on
duplicate call IDs (including after a Gateway restart) and `unknown` status for
interrupted executions. Output hashes never prove objective effects.

For the Pi container, pass the verifier-free task snapshot as `task_dir` to the
Gateway. Never mount the original task directory into a Gateway reachable by an
untrusted Agent.

## Authentication and resource bounds

`create_gateway_app` refuses to construct without a bearer token; tests must pass
`allow_unauthenticated=True` explicitly. `/v1/chat/completions` reads at most
`max_request_bytes` (default 8 MiB), optionally rejects models outside
`allowed_models`, and can cap accepted requests with `max_model_requests` (429).
Registered adapters are bounded by `tool_timeout_seconds` (default 30s); a timeout
is recorded as `tool_timeout` with `unknown` execution status and returns 504.
The standalone `harness gateway` command exposes `--allowed-model`,
`--max-model-requests` and `--tool-timeout-seconds`.

The in-process Gateway binds `0.0.0.0` because the Pi container reaches it through
`host.docker.internal:host-gateway`; the mandatory token is the network boundary.
TLS termination and interface pinning remain deployment decisions.

