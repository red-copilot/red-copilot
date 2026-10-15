import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from harness.gateway import GatewayEventWriter, ModelPricing, create_gateway_app
from harness.policy import GatewayPolicy
from harness.tool_adapter import ToolEvidenceRef, ToolExecutionContext, ToolExecutionResult


def _events(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_gateway_authentication_uses_constant_time_token_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.gateway as gateway_module

    original_compare = gateway_module.secrets.compare_digest
    compared: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(gateway_module.secrets, "compare_digest", compare)
    app = create_gateway_app(
        event_file=tmp_path / "events.jsonl",
        workspace=tmp_path,
        task_dir=tmp_path,
        gateway_token="gateway-secret",
    )

    response = TestClient(app).post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"name": "file.read", "args": {"path": "missing.txt"}},
    )

    assert response.status_code == 400
    assert compared == [(b"Bearer gateway-secret", b"Bearer gateway-secret")]


def test_gateway_writes_accounting_to_agent_inaccessible_event_file(tmp_path: Path) -> None:
    agent_events = tmp_path / "agent-workspace" / "events.jsonl"
    trusted_events = tmp_path / "gateway.events.jsonl"
    writer = GatewayEventWriter(agent_events, trusted_path=trusted_events)

    writer.emit("model.request", model="fixture", stream=False)
    writer.emit(
        "model.usage",
        model="fixture",
        input_tokens=7,
        output_tokens=5,
        total_tokens=12,
        cost_usd=0.0001,
    )
    with agent_events.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"type": "model.usage", "data": {"total_tokens": 999999, "cost_usd": 0}}
            )
            + "\n"
        )

    assert [event["type"] for event in _events(trusted_events)] == [
        "model.request",
        "model.usage",
    ]
    assert _events(agent_events)[-1]["data"]["total_tokens"] == 999999


def test_pi_rpc_accounts_trusted_gateway_usage_once_and_persists_cursor(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from harness.budget import UsageCeiling, UsageMetrics
    from harness.models import load_task
    from harness.pi_container import ContainerPiRpcSession
    from harness.session import AgentCheckpoint
    from harness.trace import TraceRecorder

    trusted_events = tmp_path / "gateway.events.jsonl"
    trusted_events.write_text(
        "\n".join(
            [
                json.dumps({"type": "model.request", "data": {"model": "fixture"}}),
                json.dumps(
                    {
                        "type": "model.usage",
                        "data": {
                            "input_tokens": 7,
                            "output_tokens": 5,
                            "total_tokens": 12,
                            "cost_usd": 0.0001,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    session = object.__new__(ContainerPiRpcSession)
    session.gateway_usage_path = trusted_events
    session._gateway_event_offset = 0
    session._metrics = UsageMetrics()
    session._budget_exceeded = None
    session.usage = UsageCeiling()
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    session._task = task.model_copy(
        update={"budgets": task.budgets.model_copy(update={"max_tokens": 10})}
    )
    session.adapter = SimpleNamespace(
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_gateway_budget", "task")
    )

    session._consume_gateway_events()
    session._consume_gateway_events()
    checkpoint = AgentCheckpoint(
        id="gateway-cursor",
        event_offset=0,
        feedback_offset=0,
        gateway_event_offset=session._gateway_event_offset,
    )

    assert session._metrics.model_calls == 1
    assert session._metrics.total_tokens == 12
    assert session._metrics.cost_usd == 0.0001
    # The trusted gateway usage crossing the declared token ceiling is charged
    # exactly once and arms the soft grace window instead of stopping the run.
    assert session._budget_exceeded is None
    assert session.usage.dimension == "max_tokens"
    assert session.usage.deadline is not None
    assert checkpoint.gateway_event_offset == trusted_events.stat().st_size
def test_tool_gateway_auth_policy_and_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = tmp_path / "task"
    workspace.mkdir()
    task_dir.mkdir()
    (task_dir / "input.txt").write_text("task-data", encoding="utf-8")
    event_file = tmp_path / "events.jsonl"

    app = create_gateway_app(
        event_file=event_file,
        workspace=workspace,
        task_dir=task_dir,
        policy=GatewayPolicy(),
        gateway_token="gateway-secret",
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer gateway-secret"}

    assert (
        client.post(
            "/v1/tools/call",
            json={"name": "file.write", "args": {"path": "proof.txt", "content": "ok"}},
        ).status_code
        == 401
    )

    written = client.post(
        "/v1/tools/call",
        headers=headers,
        json={"name": "file.write", "args": {"path": "proof.txt", "content": "ok"}},
    )
    assert written.status_code == 200
    assert (workspace / "proof.txt").read_text(encoding="utf-8") == "ok"

    read = client.post(
        "/v1/tools/call",
        headers=headers,
        json={"name": "file.read", "args": {"path": str(task_dir / "input.txt")}},
    )
    assert read.status_code == 200
    assert read.json()["result"]["content"] == "task-data"

    escaped = client.post(
        "/v1/tools/call",
        headers=headers,
        json={"name": "file.write", "args": {"path": "../escape.txt", "content": "no"}},
    )
    assert escaped.status_code == 403

    denied = client.post(
        "/v1/tools/call",
        headers=headers,
        json={"name": "shell.exec", "args": {"command": "id"}},
    )
    assert denied.status_code == 403
    event_types = [item["type"] for item in _events(event_file)]
    assert event_types.count("tool.call") == 3
    assert "tool.denied" in event_types
def test_tool_gateway_rejects_symlinked_parent_and_read_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host-secret", encoding="utf-8")
    (workspace / "linked-dir").symlink_to(outside, target_is_directory=True)
    (workspace / "secret.txt").symlink_to(outside / "secret.txt")
    client = TestClient(
        create_gateway_app(
            event_file=tmp_path / "events.jsonl",
            workspace=workspace,
            task_dir=tmp_path / "task",
            policy=GatewayPolicy(),
            gateway_token="gateway-secret",
        )
    )
    headers = {"Authorization": "Bearer gateway-secret"}

    write = client.post(
        "/v1/tools/call",
        headers=headers,
        json={
            "name": "file.write",
            "args": {"path": "linked-dir/created.txt", "content": "escaped"},
        },
    )
    read = client.post(
        "/v1/tools/call",
        headers=headers,
        json={"name": "file.read", "args": {"path": "secret.txt"}},
    )

    assert write.status_code == 400
    assert read.status_code == 400
    assert not (outside / "created.txt").exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "host-secret"


def test_registered_tool_adapter_is_allowlisted_correlated_and_untrusted(tmp_path: Path) -> None:
    class FakeAdapter:
        tool_name = "network.lookup"

        async def execute(self, arguments, context: ToolExecutionContext):
            assert arguments == {"host": "approved.example"}
            assert context.call_id == "call-network-1"
            digest = "a" * 64
            return ToolExecutionResult(
                output={"address": "192.0.2.1"},
                evidence_refs=(ToolEvidenceRef(evidence_id=f"sha256:{digest}", sha256=digest),),
                metadata={
                    "adapter": "fake-network",
                    "authorized": True,
                    "api_token": "adapter-secret",
                },
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = GatewayPolicy(allowed_tools={"file.read", "file.write", "network.lookup"})
    client = TestClient(
        create_gateway_app(
            event_file=tmp_path / "events.jsonl",
            workspace=workspace,
            task_dir=tmp_path,
            policy=policy,
            gateway_token="token",
            tool_adapters=[FakeAdapter()],
        )
    )

    response = client.post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer token"},
        json={
            "name": "network.lookup",
            "tool_call_id": "call-network-1",
            "args": {"host": "approved.example"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tool_call_id"] == "call-network-1"
    assert body["result"] == {"address": "192.0.2.1"}
    assert body["execution"]["status"] == "succeeded"
    assert body["execution"]["metadata"]["api_token"] == "[REDACTED]"
    assert body["evidence_refs"][0]["trust"] == "untrusted"
    events = _events(tmp_path / "events.jsonl")
    assert events[-1]["data"]["tool_call_id"] == "call-network-1"
    assert events[-1]["data"]["evidence_refs"][0]["trust"] == "untrusted"


def test_registered_adapter_remains_denied_without_explicit_policy(tmp_path: Path) -> None:
    class FakeAdapter:
        tool_name = "process.run"

        async def execute(self, arguments, context: ToolExecutionContext):
            raise AssertionError("denied adapter must not be invoked")

    client = TestClient(
        create_gateway_app(
            event_file=tmp_path / "events.jsonl",
            workspace=tmp_path,
            task_dir=tmp_path,
            gateway_token="token",
            tool_adapters=[FakeAdapter()],
        )
    )
    response = client.post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer token"},
        json={"name": "process.run", "args": {"argv": ["id"]}},
    )
    assert response.status_code == 403
def test_model_proxy_replaces_credentials_and_records_usage(tmp_path: Path) -> None:
    event_file = tmp_path / "events.jsonl"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer provider-secret"
        assert str(request.url) == "https://model.example/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            },
        )

    app = create_gateway_app(
        event_file=event_file,
        workspace=tmp_path,
        task_dir=tmp_path,
        gateway_token="gateway-secret",
        model_upstream="https://model.example/v1",
        model_api_key="provider-secret",
        model_pricing=ModelPricing(
            input_per_million_usd=1.0,
            output_per_million_usd=2.0,
        ),
        http_transport=httpx.MockTransport(upstream),
    )
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-secret"},
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200

    events = _events(event_file)
    assert [item["type"] for item in events] == [
        "model.request",
        "model.response",
        "model.usage",
    ]
    usage = events[-1]["data"]
    assert usage["total_tokens"] == 150
    assert usage["cost_usd"] == 0.0002
