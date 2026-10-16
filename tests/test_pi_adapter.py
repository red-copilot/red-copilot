import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent import AgentError, AgentRunContext
from harness.budget import UsageMetrics
from harness.models import AgentSpec, load_task
from harness.pi_container import (
    ContainerPiAdapter,
    ContainerPiRpcSession,
)
from harness.session import AgentEvent
from harness.trace import TraceRecorder


def _pi_spec(mode: str = "rpc", *, harness_extension: bool = False) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "pi-container-test",
            "type": "pi",
            "image": "harness-pi-kali:test",
            "network": "environment",
            "pi": {
                "provider": "fake",
                "model": "fake-model",
                "mode": mode,
                "thinking": "medium",
                "harness_extension": harness_extension,
                "env_passthrough": ["FAKE_PROVIDER_KEY"],
            },
        }
    )


def _adapter(
    monkeypatch,
    tmp_path: Path,
    mode: str = "rpc",
    *,
    harness_extension: bool = False,
) -> ContainerPiAdapter:
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    return ContainerPiAdapter(
        _pi_spec(mode, harness_extension=harness_extension),
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )


def test_pi_command_uses_rpc_mode_and_safe_defaults(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    command = adapter._command(task, gateway_enabled=False)
    assert "--mode" in command
    assert command[command.index("--mode") + 1] == "rpc"
    assert "--no-session" not in command
    assert "--no-approve" in command
    assert "--no-context-files" in command
    assert "--no-extensions" in command
    assert "--no-skills" in command
    assert "--no-mcp" in command
    assert command[command.index("--provider") + 1] == "fake"
    assert command[command.index("--model") + 1] == "fake-model"
    prompt = adapter._prompt_text(task)
    assert "$HARNESS_SUBMISSION_INBOX" in prompt
    assert "$HARNESS_FEEDBACK_FILE" in prompt
    assert "rejected candidate" in prompt
    assert "reread $HARNESS_WORLD_CONTEXT" in prompt


def test_pi_command_omits_no_extensions_only_for_the_harness_extension(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _adapter(monkeypatch, tmp_path, harness_extension=True)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    command = adapter._command(task, gateway_enabled=False)

    assert "--no-extensions" not in command
    assert "--no-skills" in command
    assert "--no-context-files" in command


def test_prepare_agent_dir_installs_the_harness_extension_when_opted_in(
    monkeypatch, tmp_path: Path
) -> None:
    enabled = _adapter(monkeypatch, tmp_path, harness_extension=True)
    run_dir = tmp_path / "enabled"
    run_dir.mkdir()
    enabled._prepare_agent_dir(run_dir=run_dir, gateway_url=None, gateway_token=None)

    asset = run_dir / "pi-agent" / "extensions" / "harness-context.ts"
    text = asset.read_text(encoding="utf-8")
    assert 'pi.on("tool_result"' in text
    assert 'pi.on("session_before_compact"' in text
    # The third hook bounds one command: the 2026-10-16 sweep lost 927s of an
    # 1800s window to a single call the Agent had asked to run unbounded.
    assert 'pi.on("tool_call"' in text
    assert "MAX_TOOL_TIMEOUT_MS = 180000" in text
    assert "event.input.timeout = MAX_TOOL_TIMEOUT_MS" in text
    assert "isToolCallEventType" in text

    disabled = _adapter(monkeypatch, tmp_path)
    other_run = tmp_path / "disabled"
    other_run.mkdir()
    disabled._prepare_agent_dir(run_dir=other_run, gateway_url=None, gateway_token=None)
    assert not (other_run / "pi-agent" / "extensions").exists()


def test_pi_container_command_is_restricted_and_passes_direct_secret(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workspace = run_dir / "agent-workspace"
    workspace.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=workspace,
        container_name="pi-test",
        network="bridge",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == (
        f"{workspace.stat().st_uid}:{workspace.stat().st_gid}"
    )
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "--cap-add" not in command
    assert "no-new-privileges" in command
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "4"
    assert "/tmp:rw,nosuid,nodev,size=512m" in command
    assert "FAKE_PROVIDER_KEY=secret" in command
    # RPC sessions read Pi's stdout, so the Agent gets its own typed-event file
    # instead of sharing the harness-normalized ``events.jsonl``.
    assert "HARNESS_EVENT_FILE=/run/harness/agent.events.jsonl" in command
    assert "HARNESS_EVENT_FILE=/run/harness/events.jsonl" not in command
    assert f"{workspace.resolve()}:/run/harness:rw" in command
    assert f"{run_dir.resolve()}:/run/harness:rw" not in command
    task_mount = next(value for value in command if value.endswith(":/task:ro"))
    assert task_mount != f"{Path('benchmarks/examples/hello').resolve()}:/task:ro"
    assert not (run_dir / "agent-task-input" / "verifier.py").exists()
    assert "HARNESS_NETWORK_PROFILE=unrestricted" in command
    assert "harness-pi-kali:test" in command


def _rpc_session_for_ingest(tmp_path: Path) -> ContainerPiRpcSession:
    session = object.__new__(ContainerPiRpcSession)
    session.run_dir = tmp_path
    session.agent_event_path = tmp_path / "agent.events.jsonl"
    session.event_path = tmp_path / "events.jsonl"
    session._agent_event_offset = 0
    session._agent_event_remainder = ""
    session._agent_event_line = 0
    session._event_offset = 0
    session.adapter = SimpleNamespace(
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task")
    )
    return session


def test_rpc_session_ingests_agent_typed_events(tmp_path: Path) -> None:
    session = _rpc_session_for_ingest(tmp_path)
    path = session.agent_event_path
    path.write_text(
        json.dumps({"type": "action.intent", "data": {"skill_id": "recon.active"}}) + "\n"
        + '{"type": "world.observe", "dat',
        encoding="utf-8",
    )

    # A trailing partial line is buffered rather than parsed.
    first = session._consume_agent_events()
    assert [event.type for event in first] == ["action.intent"]
    assert first[0].event_id == "agent-event-line:1"
    assert first[0].data == {"skill_id": "recon.active"}

    with path.open("a", encoding="utf-8") as handle:
        handle.write('a": {"type": "web.endpoint"}}\n')
    second = session._consume_agent_events()
    assert [event.type for event in second] == ["world.observe"]
    assert second[0].event_id == "agent-event-line:2"
    assert second[0].data == {"type": "web.endpoint"}

    # Malformed lines fail closed into telemetry errors instead of crashing.
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    third = session._consume_agent_events()
    assert [event.type for event in third] == ["agent.telemetry_error"]

    # Everything ingested is mirrored into the normalized artifact stream.
    persisted = [
        json.loads(line) for line in session.event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["type"] for item in persisted] == [
        "action.intent",
        "world.observe",
        "agent.telemetry_error",
    ]
    assert session._consume_agent_events() == []
def test_strict_pi_profile_clears_docker_proxy_and_rejects_proxy_passthrough(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    adapter.spec.network_profile = "model-allowed"
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-strict-proxy"
    run_dir.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="pi-strict-proxy",
        network="gateway-internal",
        environment_project=None,
        seed=1,
        gateway_url="http://harness-gateway:8765",
        gateway_token="gateway-token",
        host_gateway=False,
    )
    assert "HTTP_PROXY=" in command
    assert "https_proxy=" in command

    adapter.pi.env_passthrough.append("ALL_PROXY")
    with pytest.raises(AgentError, match="strict network profile.*proxy"):
        adapter._container_command(
            task,
            task_dir=Path("benchmarks/examples/hello").resolve(),
            run_dir=run_dir,
            container_name="pi-strict-proxy",
            network="gateway-internal",
            environment_project=None,
            seed=1,
            gateway_url="http://harness-gateway:8765",
            gateway_token="gateway-token",
            host_gateway=False,
        )
def test_pi_usage_with_malformed_counters_is_preserved_for_fail_closed_handling(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _adapter(monkeypatch, tmp_path)

    normalized = adapter._normalize_line(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input": "invalid",
                        "output": 2,
                        "totalTokens": -10,
                        "cost": {"total": 0.1},
                    },
                },
            }
        ),
        account_model=True,
    )

    assert normalized[-1][0] == "model.usage"
    assert normalized[-1][1]["input_tokens"] == "invalid"
    assert normalized[-1][1]["total_tokens"] == -10

    session = object.__new__(ContainerPiRpcSession)
    session.adapter = adapter
    session._metrics = UsageMetrics()
    session._budget_exceeded = None
    session._account(AgentEvent(type="model.usage", data=normalized[-1][1]))

    assert session._budget_exceeded == "invalid_telemetry"
    assert session._metrics.total_tokens == 0


def test_gateway_mode_accounts_model_usage_from_gateway_only(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    session = ContainerPiRpcSession(
        adapter,
        context=AgentRunContext(
            run_dir=tmp_path,
            task=load_task(Path("benchmarks/examples/hello/task.yaml")),
            task_dir=tmp_path,
            gateway_url="http://gateway:8765",
            gateway_token="test-token",
            gateway_usage_path=tmp_path / "gateway.events.jsonl",
        ),
    )

    pi_events = session._normalize_pi_record(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 3, "output": 2, "totalTokens": 5},
                },
            }
        )
    )
    session._consume_gateway_event_line(
        json.dumps({"type": "model.request", "data": {}}).encode() + b"\n"
    )
    session._consume_gateway_event_line(
        json.dumps(
            {
                "type": "model.usage",
                "data": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                    "cost_usd": 0.01,
                },
            }
        ).encode()
        + b"\n"
    )

    assert pi_events == []
    assert session._metrics.model_calls == 1
    assert session._metrics.total_tokens == 5
    assert session._metrics.cost_usd == 0.01


@pytest.mark.parametrize(
    ("budget_name", "limit", "records", "event_types", "metric_name", "metric_value"),
    [
        (
            "max_tokens",
            2,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 2, "output": 2, "totalTokens": 4},
                    },
                }
            ],
            ["pi.session", "model.response", "model.usage", "pi.agent_settled"],
            "total_tokens",
            4,
        ),
        (
            "max_model_calls",
            1,
            [
                {"type": "message_start", "message": {"role": "assistant"}},
                {"type": "message_start", "message": {"role": "assistant"}},
            ],
            ["pi.session", "model.request", "model.request", "pi.agent_settled"],
            "model_calls",
            2,
        ),
        (
            "max_tool_calls",
            1,
            [
                {"type": "tool_execution_start", "toolCallId": "tool-1", "toolName": "bash"},
                {"type": "tool_execution_start", "toolCallId": "tool-2", "toolName": "bash"},
            ],
            ["pi.session", "tool.call", "tool.call", "pi.agent_settled"],
            "tool_calls",
            2,
        ),
        (
            "max_cost_usd",
            0.005,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {
                            "input": 2,
                            "output": 2,
                            "totalTokens": 4,
                            "cost": {"total": 0.01},
                        },
                    },
                }
            ],
            ["pi.session", "model.response", "model.usage", "pi.agent_settled"],
            "cost_usd",
            0.01,
        ),
    ],
)
def test_pi_rpc_session_accounts_usage_with_soft_budget_crossing(
    monkeypatch,
    tmp_path: Path,
    budget_name: str,
    limit: float,
    records: list[dict],
    event_types: list[str],
    metric_name: str,
    metric_value: float,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-key")
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    task = task.model_copy(
        update={"budgets": task.budgets.model_copy(update={budget_name: limit})}
    )
    run_dir = tmp_path / "run-budget-rpc"
    run_dir.mkdir()
    killed: list[str] = []

    class FakeWriter:
        def write(self, value: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class FakeReader:
        def __init__(self) -> None:
            self.lines = [{"type": "session", "id": "budget-session"}, *records]
            self.lines.append({"type": "agent_settled"})

        async def readline(self) -> bytes:
            if not self.lines:
                return b""
            return (json.dumps(self.lines.pop(0)) + "\n").encode()

    class FakeProcess:
        stdin = FakeWriter()
        stdout = FakeReader()
        returncode = None

        async def wait(self) -> int:
            self.returncode = -9
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(
        adapter,
        "_docker",
        lambda command, *, cwd, check=True: type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    monkeypatch.setattr(
        "harness.pi_container.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: asyncio.sleep(0, result=fake_process),
    )

    def fake_run(command, **kwargs):
        if command[1] == "kill":
            killed.append(command[-1])
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("harness.pi_container.subprocess.run", fake_run)

    async def scenario():
        session = await ContainerPiRpcSession.start(
            adapter,
            context=AgentRunContext(
                task=task,
                task_dir=Path("benchmarks/examples/hello").resolve(),
                run_dir=run_dir,
                environment_project=None,
                environment_network=None,
                seed=1,
            ),
        )
        events = [event async for event in session.events()]
        result = await session.result()
        return events, result

    events, result = asyncio.run(scenario())

    assert [event.type for event in events] == event_types
    # Crossing a usage ceiling is soft: the crossing is traced, the stream keeps
    # flowing through the settle, usage stays charged, and only the grace
    # window (not the crossing itself) can end the session. (The kill in the
    # teardown log is result_drain, the normal post-stream cleanup.)
    trace_text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"budget.exceeded"' in trace_text
    assert f'"budget":"{budget_name}"' in trace_text
    assert result.budget_exceeded is None
    assert result.timed_out is False
    assert getattr(result.metrics, metric_name) == metric_value


def test_pi_rpc_session_kills_container_after_usage_grace_window(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-key")
    # The usage ceiling arms the shared budget grace window (defined in
    # harness.budget); the container's wall-clock deadline reads the imported
    # name, so patching the definition and the import keeps one short window.
    monkeypatch.setattr("harness.budget.FINALIZE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr("harness.pi_container.FINALIZE_GRACE_SECONDS", 0.2)
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    task = task.model_copy(
        update={"budgets": task.budgets.model_copy(update={"max_tokens": 2})}
    )
    run_dir = tmp_path / "run-budget-grace"
    run_dir.mkdir()
    killed: list[str] = []

    class FakeWriter:
        def write(self, value: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class BlockingReader:
        def __init__(self) -> None:
            self.lines = [
                {"type": "session", "id": "budget-grace-session"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 2, "output": 2, "totalTokens": 4},
                    },
                },
            ]

        async def readline(self) -> bytes:
            if self.lines:
                return (json.dumps(self.lines.pop(0)) + "\n").encode()
            await asyncio.sleep(30)  # never settles: the deadline must fire
            return b""

    class FakeProcess:
        stdin = FakeWriter()
        stdout = BlockingReader()
        returncode = None

        async def wait(self) -> int:
            self.returncode = -9
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(
        adapter,
        "_docker",
        lambda command, *, cwd, check=True: type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    monkeypatch.setattr(
        "harness.pi_container.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: asyncio.sleep(0, result=fake_process),
    )

    def fake_run(command, **kwargs):
        if command[1] == "kill":
            killed.append(command[-1])
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("harness.pi_container.subprocess.run", fake_run)

    async def scenario():
        session = await ContainerPiRpcSession.start(
            adapter,
            context=AgentRunContext(
                task=task,
                task_dir=Path("benchmarks/examples/hello").resolve(),
                run_dir=run_dir,
                environment_project=None,
                environment_network=None,
                seed=1,
            ),
        )
        events = [event async for event in session.events()]
        result = await session.result()
        return events, result

    events, result = asyncio.run(scenario())

    assert [event.type for event in events] == [
        "pi.session",
        "model.response",
        "model.usage",
    ]
    assert killed, "the container must be killed once the grace window elapsed"
    assert result.budget_exceeded == "max_tokens"
    assert result.metrics.total_tokens == 4
def test_pi_rpc_uses_verified_target_network_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-provider-key")
    adapter = _adapter(monkeypatch, tmp_path)
    adapter.spec.network_profile = "target-only"
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-strict-rpc"
    run_dir.mkdir()
    agent_task_dir = tmp_path / "agent-task"
    agent_task_dir.mkdir()
    docker_commands = []

    class Reader:
        async def readline(self):
            return b'{"type":"session","id":"session-1"}\n'

    class Process:
        stdout = Reader()
        stdin = object()
        returncode = None

    def inspect(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{'b' * 64} true\n", stderr=""
        )

    monkeypatch.setattr("harness.network_policy.subprocess.run", inspect)
    monkeypatch.setattr(adapter, "_prepare_agent_dir", lambda **kwargs: None)
    monkeypatch.setattr(
        adapter,
        "_docker",
        lambda command, **kwargs: docker_commands.append(command)
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "harness.pi_container.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: asyncio.sleep(0, result=Process()),
    )

    async def start_session():
        session = await ContainerPiRpcSession.start(
            adapter,
            context=AgentRunContext(
                task=task,
                task_dir=Path("benchmarks/examples/hello").resolve(),
                agent_task_dir=agent_task_dir,
                run_dir=run_dir,
                environment_project=None,
                environment_network="task-internal",
                seed=1,
            ),
        )
        session._stderr_handle.close()

    asyncio.run(start_session())

    create = next(command for command in docker_commands if "--network" in command)
    assert create[create.index("--network") + 1] == "b" * 64
    # A fresh session first inspects for a container left by a previous session
    # in the same run directory (context restart), then creates its own.
    assert len(docker_commands) == 2
    assert docker_commands[0][1] == "inspect"
def test_pi_rpc_normalizes_a_coarse_command_family() -> None:
    from harness.pi_adapter import _command_family

    assert _command_family("bash", {"command": "for id in ../../etc/passwd; do curl $id; done"}) == (
        "bash:for"
    )
    assert _command_family("bash", {"command": "TARGET=x ffuf -w /w -u http://$TARGET"}) == (
        "bash:ffuf"
    )
    assert _command_family("bash", {"command": "/usr/bin/curl -s http://x"}) == "bash:curl"
    assert _command_family("bash", {"command": "cd /tmp; for id in a b; do curl $id; done"}) == (
        "bash:for"
    )
    assert _command_family("bash", {"command": "timeout 60 curl -s http://x"}) == "bash:curl"
    assert _command_family(
        "bash", {"command": "cd /tmp\n# enumerate endpoints\nffuf -u http://x/FUZZ -w /w"}
    ) == "bash:ffuf"
    assert _command_family("read", {}) is None


def test_pi_tool_output_feeds_fact_extraction(monkeypatch, tmp_path: Path) -> None:
    from harness.extract import extract_facts

    adapter = _adapter(monkeypatch, tmp_path)
    raw = json.dumps(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": "ignored"}],
                "structuredContent": {
                    "output": (
                        "admin  [Status: 200, Size: 5]\n"
                        "http://10.0.0.5/api  (Status: 403) [Size: 0]\n"
                        "80/tcp   open  http    Apache httpd\n"
                    ),
                    "exit_code": 0,
                },
            },
        }
    )
    normalized = adapter._normalize_line(raw, account_model=True)
    assert [event_type for event_type, _ in normalized] == ["tool.result"]
    data = normalized[0][1]
    assert data["exit_code"] == 0
    assert data["output_excerpt"].startswith("admin")

    facts = extract_facts(data)
    assert {fact["type"] for fact in facts} == {"web.endpoint", "network.service"}
    assert len(facts) == 3
