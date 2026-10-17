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

For container Agents, pass the verifier-free task snapshot as `task_dir` to the
Gateway. Never mount the original task directory into a Gateway reachable by an
untrusted Agent.

