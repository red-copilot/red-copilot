import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.agent import AgentError, AgentRunContext
from harness.budget import UsageCeiling, UsageMetrics
from harness.models import AgentSpec, load_task
from harness.pi_container import (
    ContainerPiAdapter,
    ContainerPiRpcSession,
)
from harness.session import AgentEvent, AgentObservation
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


def test_resume_refuses_to_replace_running_pi_container(tmp_path: Path) -> None:
    calls = []

    def docker(command, *, cwd, check=True):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")

    session = object.__new__(ContainerPiRpcSession)
    session.adapter = SimpleNamespace(_docker=docker)
    session.container_name = "harness_pi_run"

    with pytest.raises(AgentError, match="still running"):
        asyncio.run(session._guard_existing_resume_container(tmp_path))

    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "inspect", "--format"]


def test_resume_removes_only_a_stopped_pi_container(tmp_path: Path) -> None:
    calls = []

    def docker(command, *, cwd, check=True):
        calls.append(command)
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout="false\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    session = object.__new__(ContainerPiRpcSession)
    session.adapter = SimpleNamespace(_docker=docker)
    session.container_name = "harness_pi_run"

    asyncio.run(session._guard_existing_resume_container(tmp_path))

    assert [call[1] for call in calls] == ["inspect", "rm"]


def test_pi_rpc_session_rejects_unsafe_or_unexpected_startup_session_id() -> None:
    class _Reader:
        def __init__(self, session_id):
            self.session_id = session_id

        async def readline(self):
            return (json.dumps({"type": "session", "id": self.session_id}) + "\n").encode()

    async def verify(session_id, requested_id=None):
        session = object.__new__(ContainerPiRpcSession)
        session.process = SimpleNamespace(stdout=_Reader(session_id))
        session.context = SimpleNamespace(resume_session_id=requested_id)
        session._startup_records = []
        session._pi_session_id = None
        await session._wait_for_session_identity(timeout=1.0, required=True)

    with pytest.raises(AgentError, match="invalid session ID"):
        asyncio.run(verify("../unsafe"))
    with pytest.raises(AgentError, match="did not restore"):
        asyncio.run(verify("new-session", "expected-session"))


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


def test_pi_rpc_command_can_select_a_checkpointed_session(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    command = adapter._command(task, gateway_enabled=False, session_id="pi-session-123")

    assert command[command.index("--session") + 1] == "pi-session-123"
    try:
        adapter._command(task, gateway_enabled=False, session_id="../../etc/passwd")
    except AgentError as exc:
        assert "invalid Pi session ID" in str(exc)
    else:
        raise AssertionError("unsafe session references must be rejected")


def test_pi_json_mode_remains_available_for_compatibility(monkeypatch, tmp_path: Path) -> None:
    spec = _pi_spec(mode="json")
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    adapter = ContainerPiAdapter(
        spec,
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    command = adapter._command(task, gateway_enabled=False)

    assert command[command.index("--mode") + 1] == "json"
    assert command[-1].startswith(task.objective.description)


def test_pi_gateway_writes_container_visible_provider(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    agent_dir = adapter._prepare_agent_dir(
        run_dir=tmp_path,
        gateway_url="http://harness-gateway:8765",
        gateway_token="one-time-token",
    )
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    provider = models["providers"]["harness"]
    assert provider["baseUrl"] == "http://harness-gateway:8765/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "$HARNESS_GATEWAY_TOKEN"


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
    # Root inside the container: the workspace keeps the harness identity as
    # owner and widens to 0755/0644 so its root-written artifacts stay readable.
    assert command[command.index("--user") + 1] == "0:0"
    assert workspace.stat().st_uid == os.getuid()
    assert command[command.index("--cap-drop") + 1] == "ALL"
    # Every Agent container carries the cap-dropped-root baseline; NET_RAW is
    # only added when the Agent spec declares it.
    assert [
        command[index + 1]
        for index, entry in enumerate(command)
        if entry == "--cap-add"
    ] == ["DAC_OVERRIDE", "FOWNER"]
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


def test_json_mode_container_command_keeps_the_shared_event_file(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _adapter(monkeypatch, tmp_path, mode="json")
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-json"
    run_dir.mkdir()
    workspace = run_dir / "agent-workspace"
    workspace.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=workspace,
        container_name="pi-json-test",
        network="bridge",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )
    assert "HARNESS_EVENT_FILE=/run/harness/events.jsonl" in command
    assert "HARNESS_EVENT_FILE=/run/harness/agent.events.jsonl" not in command


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


def test_rpc_session_agent_event_cursor_recovers_from_truncation(tmp_path: Path) -> None:
    session = _rpc_session_for_ingest(tmp_path)
    path = session.agent_event_path
    path.write_text(
        json.dumps({"type": "action.intent", "data": {"note": "x" * 200}}) + "\n",
        encoding="utf-8",
    )
    assert [event.type for event in session._consume_agent_events()] == ["action.intent"]

    # The Agent replaced its file with a shorter one: restart from the top and
    # record the reset rather than reading from a stale offset.
    path.write_text(json.dumps({"type": "world.observe", "data": {}}) + "\n", encoding="utf-8")
    assert [event.type for event in session._consume_agent_events()] == ["world.observe"]


def test_rpc_session_rejects_oversized_agent_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harness.pi_container.MAX_AGENT_EVENT_LINE_BYTES", 64)
    session = _rpc_session_for_ingest(tmp_path)
    session.agent_event_path.write_text("x" * 200 + "\n", encoding="utf-8")
    events = session._consume_agent_events()
    assert [event.type for event in events] == ["agent.telemetry_error"]


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


def test_pi_only_restores_explicitly_configured_capabilities(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    adapter.pi.cap_add = ["NET_RAW"]
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-capability"
    run_dir.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="pi-explicit-capability",
        network="bridge",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )

    assert "NET_RAW" in command


def test_pi_launch_rechecks_reserved_environment_names(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    adapter.pi.env_passthrough = ["BENCHMARK_TOKEN"]
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-reserved-env"
    run_dir.mkdir()

    with pytest.raises(AgentError, match="reserved credential environment"):
        adapter._container_command(
            task,
            task_dir=Path("benchmarks/examples/hello").resolve(),
            run_dir=run_dir,
            container_name="pi-reserved-env",
            network="none",
            environment_project=None,
            seed=1,
            gateway_url=None,
            gateway_token=None,
            host_gateway=False,
        )


def test_pi_gateway_does_not_pass_direct_provider_secret(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="pi-test",
        network="gateway-net",
        environment_project=None,
        seed=1,
        gateway_url="http://harness-gateway:8765",
        gateway_token="one-time-token",
        host_gateway=False,
    )
    assert "FAKE_PROVIDER_KEY=secret" not in command
    assert "HARNESS_GATEWAY_TOKEN=one-time-token" in command


def test_pi_json_protocol_normalizes_model_and_tool_events(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    event_file = tmp_path / "events.jsonl"
    event_file.write_text("", encoding="utf-8")
    records = [
        {"type": "session", "version": 3, "id": "s1", "cwd": "/run/harness"},
        {
            "type": "message_start",
            "message": {"role": "assistant", "provider": "fake", "model": "fake-model"},
        },
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash"},
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "isError": False,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "fake",
                "model": "fake-model",
                "stopReason": "stop",
                "usage": {
                    "input": 10,
                    "output": 5,
                    "totalTokens": 15,
                    "cost": {"total": 0.01},
                },
            },
        },
        {"type": "agent_settled"},
    ]
    for record in records:
        adapter._translate_line(
            json.dumps(record),
            event_file=event_file,
            account_model=True,
        )

    types = [
        json.loads(line)["type"] for line in event_file.read_text(encoding="utf-8").splitlines()
    ]
    assert types == [
        "pi.session",
        "model.request",
        "tool.call",
        "tool.result",
        "model.response",
        "model.usage",
        "pi.agent_settled",
    ]


def test_pi_usage_with_malformed_counters_degrades_without_terminating(
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
    session._telemetry_invalid = None
    session._account(AgentEvent(type="model.usage", data=normalized[-1][1]))

    # Invalid telemetry degrades accounting instead of killing the session.
    assert session._budget_exceeded is None
    assert session._telemetry_invalid == "invalid or negative Pi usage telemetry"
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


def test_pi_message_end_without_usage_emits_missing_usage_event(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)

    normalized = adapter._normalize_line(
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "provider": "fixture"},
            }
        ),
        account_model=True,
    )

    assert [event_type for event_type, _data in normalized] == [
        "model.response",
        "model.usage_missing",
    ]


def test_pi_rpc_degrades_missing_usage_event_without_terminating(
    monkeypatch, tmp_path: Path
) -> None:
    from harness.budget import UsageMetrics

    adapter = _adapter(monkeypatch, tmp_path)
    session = object.__new__(ContainerPiRpcSession)
    session.adapter = adapter
    session._metrics = UsageMetrics()
    session._budget_exceeded = None
    session._telemetry_invalid = None

    session._account(AgentEvent(type="model.usage_missing", data={"source": "pi"}))

    assert session._budget_exceeded is None
    assert session._telemetry_invalid == "model usage telemetry was missing"
    assert session._metrics.total_tokens == 0


def test_pi_usage_degrades_total_below_input_and_output_sum(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    session = object.__new__(ContainerPiRpcSession)
    session.adapter = adapter
    session._metrics = UsageMetrics()
    session._budget_exceeded = None
    session._telemetry_invalid = None

    session._account(
        AgentEvent(
            type="model.usage",
            data={"input_tokens": 6, "output_tokens": 5, "total_tokens": 1},
        )
    )

    assert session._budget_exceeded is None
    assert session._telemetry_invalid == "invalid or negative Pi usage telemetry"
    assert session._metrics.total_tokens == 0


def test_pi_rpc_session_sends_prompt_and_steers_before_settled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-key")
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-rpc"
    run_dir.mkdir()
    writes: list[bytes] = []

    class FakeWriter:
        closed = False

        def write(self, value: bytes) -> None:
            writes.append(value)

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.closed = True

    class FakeReader:
        def __init__(self) -> None:
            self.lines = [
                {"type": "session", "id": "session-1", "version": 1},
                b"not-json rpc record\n",
                {
                    "type": "response",
                    "id": "command-1",
                    "success": False,
                    "error": "api_key=provider-secret",
                },
                {
                    "type": "message_start",
                    "message": {"role": "assistant", "provider": "fake", "model": "fake-model"},
                },
                {"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash"},
                {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "bash"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": "fake",
                        "model": "fake-model",
                        "usage": {"input": 3, "output": 2, "totalTokens": 5},
                    },
                },
                {"type": "agent_end"},
                {"type": "agent_settled"},
                {
                    "type": "message_start",
                    "message": {"role": "assistant", "provider": "fake", "model": "fake-model"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": "fake",
                        "model": "fake-model",
                        "usage": {"input": 4, "output": 1, "totalTokens": 5},
                    },
                },
                {"type": "agent_end"},
                {"type": "agent_settled"},
            ]

        async def readline(self) -> bytes:
            if not self.lines:
                return b""
            line = self.lines.pop(0)
            return line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode()

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeWriter()
            self.stdout = FakeReader()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

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
    monkeypatch.setattr(
        "harness.pi_container.subprocess.run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 0})(),
    )

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
        initial_checkpoint = await session.checkpoint()
        assert initial_checkpoint.session_id == "session-1"
        await session.observe(
            AgentObservation(type="solver.plan.updated", data={"actions": ["probe"]})
        )
        events = []
        settled_count = 0
        async for event in session.events():
            events.append(event)
            if event.type == "tool.call":
                await session.observe(
                    AgentObservation(
                        type="solver.verification",
                        data={"status": "pending", "replan_required": True},
                    )
                )
            elif event.type == "pi.agent_settled":
                settled_count += 1
                if settled_count == 1:
                    await session.observe(
                        AgentObservation(
                            type="benchmark.feedback",
                            data={"accepted": False, "completed": False},
                        )
                    )
        result = await session.result()
        return events, result

    events, result = asyncio.run(scenario())
    commands = [json.loads(line) for line in b"".join(writes).decode().splitlines()]

    assert commands[0]["type"] == "set_steering_mode"
    assert commands[0]["mode"] == "all"
    assert commands[1]["type"] == "prompt"
    assert "solver.plan.updated" in commands[1]["message"]
    assert commands[2]["type"] == "steer"
    assert "solver.verification" in commands[2]["message"]
    assert commands[3]["type"] == "prompt"
    assert "benchmark.feedback" in commands[3]["message"]
    assert [event.type for event in events] == [
        "pi.session",
        "agent.telemetry_error",
        "agent.telemetry_error",
        "model.request",
        "tool.call",
        "tool.result",
        "model.response",
        "model.usage",
        "pi.agent_settled",
        "model.request",
        "model.response",
        "model.usage",
        "pi.agent_settled",
    ]
    assert len({event.event_id for event in events}) == len(events)
    assert result.metrics.tool_calls == 1
    assert result.metrics.total_tokens == 10
    rpc_error = events[2]
    assert rpc_error.data == {
        "source": "pi.rpc",
        "command_id_sha256": hashlib.sha256(b"command-1").hexdigest(),
        "error_type": "str",
        "message": "Pi RPC command failed",
    }
    assert "provider-secret" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "provider-secret" not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


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
def test_pi_rpc_session_accounts_usage_without_enforcing_non_time_limits(
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
    # Usage above a declared ceiling is still charged, but the crossing is
    # soft: it is traced and arms the finalize grace window instead of
    # stopping the stream immediately.
    assert result.budget_exceeded is None
    assert result.timed_out is False
    trace_text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert '"budget.exceeded"' in trace_text
    assert f'"budget":"{budget_name}"' in trace_text
    assert getattr(result.metrics, metric_name) == metric_value


def test_pi_offline_network_profile_forces_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    spec = _pi_spec().model_copy(update={"network_profile": "offline"})
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    adapter = ContainerPiAdapter(
        spec,
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-offline"
    run_dir.mkdir()

    command = adapter._container_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="pi-offline",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )
    assert command[command.index("--network") + 1] == "none"
    assert "HARNESS_NETWORK_PROFILE=offline" in command


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

def test_legacy_pi_json_runner_uses_verified_target_network_id(monkeypatch, tmp_path: Path) -> None:
    from harness.pi_adapter import PiAdapter

    adapter_spec = _pi_spec(mode="json").model_copy(
        update={"network_profile": "target-only"}
    )
    monkeypatch.setattr("harness.pi_adapter.shutil.which", lambda _: "/usr/bin/docker")
    adapter = PiAdapter(
        adapter_spec,
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    run_dir = tmp_path / "run-strict-json"
    run_dir.mkdir()
    commands = []

    def fake_subprocess_run(command, **kwargs):
        commands.append(command)
        stdout = f"{'c' * 64} true\n" if command[:3] == ["docker", "network", "inspect"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    class Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    monkeypatch.setattr("harness.network_policy.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("harness.pi_adapter.subprocess.Popen", lambda *args, **kwargs: Process())

    adapter.run(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        environment_project=None,
        environment_network="task-internal",
        seed=1,
    )

    create = next(command for command in commands if command[:2] == ["docker", "create"])
    assert create[create.index("--network") + 1] == "c" * 64


def _adapter_with_system_prompt(monkeypatch, tmp_path: Path, prompt_file: str) -> ContainerPiAdapter:
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "pi-system-prompt",
            "type": "pi",
            "image": "harness-pi-kali:test",
            "network": "environment",
            "pi": {
                "provider": "fake",
                "model": "fake-model",
                "system_prompt_file": prompt_file,
            },
        }
    )
    return ContainerPiAdapter(
        spec,
        allow_host=False,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )


def test_pi_system_prompt_file_is_prepended(monkeypatch, tmp_path: Path) -> None:
    prompt_file = tmp_path / "solver.md"
    prompt_file.write_text("CONTROL-LOOP-INSTRUCTIONS\n", encoding="utf-8")
    adapter = _adapter_with_system_prompt(monkeypatch, tmp_path, str(prompt_file))
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    prompt = adapter._prompt_text(task)

    assert prompt.startswith("CONTROL-LOOP-INSTRUCTIONS")
    assert "$HARNESS_SUBMISSION_INBOX" in prompt
    assert task.objective.description in prompt


def test_pi_missing_system_prompt_file_fails_closed(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter_with_system_prompt(
        monkeypatch, tmp_path, str(tmp_path / "missing.md")
    )
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    with pytest.raises(AgentError, match="system prompt file was not found"):
        adapter._prompt_text(task)


def test_pi_rpc_normalizes_a_coarse_command_family() -> None:
    from harness.pi_adapter import _command_family

    assert _command_family("bash", {"command": "for id in ../../etc/passwd; do curl $id; done"}) == (
        "bash:curl"
    )
    assert _command_family("bash", {"command": "TARGET=x ffuf -w /w -u http://$TARGET"}) == (
        "bash:ffuf"
    )
    assert _command_family("bash", {"command": "/usr/bin/curl -s http://x"}) == "bash:curl"
    assert _command_family("bash", {"command": "cd /tmp; for id in a b; do curl $id; done"}) == (
        "bash:curl"
    )
    assert _command_family("bash", {"command": "timeout 60 curl -s http://x"}) == "bash:curl"
    assert _command_family(
        "bash", {"command": "cd /tmp\n# enumerate endpoints\nffuf -u http://x/FUZZ -w /w"}
    ) == "bash:ffuf"
    assert _command_family("bash", {"command": "echo hello > /tmp/a"}) is None
    assert _command_family("bash", {"command": "cat notes.txt | grep admin"}) is None
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


def test_pi_rpc_session_survives_missing_usage_mid_stream(monkeypatch, tmp_path: Path) -> None:
    """A provider response without usage data must not end the session.

    Only wall_time is enforced; missing usage degrades accounting for that one
    call instead of killing a one-shot evaluation run.
    """
    import asyncio

    from harness.budget import UsageMetrics
    from harness.trace import TraceRecorder

    adapter = _adapter(monkeypatch, tmp_path)
    trace = TraceRecorder(tmp_path / "trace.jsonl", "run-tel", "task-tel")
    adapter.trace = trace
    session = object.__new__(ContainerPiRpcSession)
    session.adapter = adapter
    session._metrics = UsageMetrics()
    session._budget_exceeded = None
    session._telemetry_invalid = None
    session._task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    session.usage = UsageCeiling()

    session._account(AgentEvent(type="model.usage_missing", data={"source": "pi"}))
    session._account(
        AgentEvent(
            type="model.usage",
            data={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
        )
    )

    # The session kept running: later usage still accumulates.
    assert session._metrics.total_tokens == 7
    assert session._budget_exceeded is None
    rows = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["type"] == "agent.telemetry_error" for row in rows)
    assert not any(row["type"] == "budget.exceeded" for row in rows)
    del asyncio
