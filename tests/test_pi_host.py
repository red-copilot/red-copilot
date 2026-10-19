from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.agent import AgentRunContext, build_agent_adapter
from harness.models import AgentSpec, load_task
from harness.pi_host import HostPiAdapter
from harness.trace import TraceRecorder


def _hosted_spec(**overrides) -> AgentSpec:
    pi_overrides = overrides.pop("pi", {})
    pi = {
        "mode": "json",
        "model": "deepseek-chat",
        "tools": ["read", "bash", "edit", "write"],
        "models_base_url": "http://api.deepseek.com.tsecbench.gw",
        "models_api_key_env": "DEEPSEEK_API_KEY",
    }
    pi.update(pi_overrides)
    payload = {
        "apiVersion": "harness/v1",
        "id": "pi-hosted-test",
        "type": "pi-host",
        "pi": pi,
    }
    payload.update(overrides)
    return AgentSpec.model_validate(payload)


def _adapter(monkeypatch, tmp_path: Path, **overrides) -> HostPiAdapter:
    monkeypatch.setattr("harness.pi_host.shutil.which", lambda _: "/usr/bin/pi")
    return HostPiAdapter(
        _hosted_spec(**overrides),
        allow_host=True,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )


def _context(task: object, tmp_path: Path) -> AgentRunContext:
    run_dir = tmp_path / "run-host"
    run_dir.mkdir(exist_ok=True)
    return AgentRunContext(
        task=task,
        task_dir=tmp_path,
        run_dir=run_dir,
        seed=1,
    )


def test_pi_host_spec_rejects_partial_gateway_config() -> None:
    with pytest.raises(ValueError, match="models_base_url and models_api_key_env together"):
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-hosted-partial",
                "type": "pi-host",
                "pi": {
                    "mode": "json",
                    "model": "deepseek-chat",
                    "models_api_key_env": "DEEPSEEK_API_KEY",
                },
            }
        )
    with pytest.raises(ValueError, match="not a container"):
        _hosted_spec(image="harness/pi-kali:local")


def test_pi_host_requires_allow_host_and_json_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.pi_host.shutil.which", lambda _: "/usr/bin/pi")
    with pytest.raises(Exception, match="allow-host-agent"):
        HostPiAdapter(
            _hosted_spec(),
            allow_host=False,
            trace=TraceRecorder(tmp_path / "t.jsonl", "r", "t"),
        )
    with pytest.raises(Exception, match="pi.mode: json"):
        HostPiAdapter(
            _hosted_spec(pi={"mode": "rpc"}),
            allow_host=True,
            trace=TraceRecorder(tmp_path / "t.jsonl", "r", "t"),
        )


def test_pi_host_builds_gateway_models_json_and_command(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    agent_dir = adapter._prepare_hosted_models(tmp_path)
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    assert models["providers"]["hosted"]["baseUrl"] == "http://api.deepseek.com.tsecbench.gw"
    assert models["providers"]["hosted"]["apiKey"] == "${DEEPSEEK_API_KEY}"
    assert models["providers"]["hosted"]["api"] == "openai-completions"

    command = adapter._hosted_command(task)
    assert command[0] == "pi"
    assert command[command.index("--provider") + 1] == "hosted"
    assert command[command.index("--model") + 1] == "deepseek-chat"
    # json mode carries the objective prompt as the final argument
    assert "red-harness-ok" in command[-1]


def test_pi_host_run_refuses_empty_credential(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(Exception, match="DEEPSEEK_API_KEY"):
        adapter.run(_context(task, tmp_path))


def test_pi_host_run_normalizes_pi_jsonl(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict = {}

    class Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    pi_jsonl = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 4, "output": 5, "totalTokens": 9},
                        "cost": {"total": 0.01},
                    },
                }
            ),
            json.dumps({"type": "agent_end"}),
        ]
    )

    def fake_popen(command, **kwargs):
        captured["argv"] = command
        captured["env"] = kwargs.get("env")
        raw = Path(kwargs["cwd"]) / "agent.stdout.log"
        raw.write_text(pi_jsonl + "\n", encoding="utf-8")
        return Process()

    monkeypatch.setattr("harness.pi_host.subprocess.Popen", fake_popen)

    result = adapter.run(_context(task, tmp_path))

    assert result.returncode == 0
    assert result.metrics.total_tokens == 9
    env = captured["env"]
    assert env["PI_TELEMETRY"] == "0"
    assert env["HARNESS_RUN_DIR"].endswith("run-host")
    events = [
        json.loads(line)
        for line in (tmp_path / "run-host" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    types = [event["type"] for event in events]
    assert "model.usage" in types


def test_build_agent_adapter_dispatches_pi_host(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.pi_host.shutil.which", lambda _: "/usr/bin/pi")
    adapter = build_agent_adapter(
        _hosted_spec(),
        allow_host_agent=True,
        trace=TraceRecorder(tmp_path / "t.jsonl", "r", "t"),
    )
    assert isinstance(adapter, HostPiAdapter)


def test_subagent_extracts_assistant_text_from_pi_jsonl() -> None:
    from harness.pi_host import _assistant_text

    stream = "\n".join(
        [
            json.dumps({"type": "session", "id": "s1"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first reply"}],
                    },
                }
            ),
            json.dumps({"type": "tool_execution_start"}),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall"},
                            {"type": "text", "text": "FINDINGS: offset is 136"},
                        ],
                    },
                }
            ),
            "not json",
        ]
    )

    assert _assistant_text(stream) == "first reply\n\nFINDINGS: offset is 136"


def test_run_subagent_isolates_environment_and_briefs_the_task(monkeypatch, tmp_path: Path) -> None:

    from harness.pi_host import SUBAGENT_SYSTEM_PROMPT, run_subagent

    monkeypatch.setattr("harness.pi_host.shutil.which", lambda _: "/usr/bin/pi")
    monkeypatch.setenv("HARNESS_WORLD_CONTEXT", "/parent/world.txt")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict = {}

    class Process:
        returncode = 0
        pid = -1

        def communicate(self, timeout=None):
            if captured.get("fail_timeout"):
                raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)
            stream = json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "FINDINGS: done"}],
                    },
                }
            )
            return stream.encode(), b""

        def kill(self):
            pass

    def fake_popen(command, **kwargs):
        captured["argv"] = command
        captured["env"] = kwargs.get("env")
        return Process()

    monkeypatch.setattr("harness.pi_host.subprocess.Popen", fake_popen)
    spec = _hosted_spec()

    result = run_subagent(
        spec, "triage the binary", timeout=60, cwd=tmp_path / "work"
    )

    # The positional prompt is the sub-agent briefing plus the task, and the
    # parent's solver prompt file never leaks in.
    prompt = captured["argv"][-1]
    assert SUBAGENT_SYSTEM_PROMPT.split(".")[0] in prompt
    assert "triage the binary" in prompt
    assert "Harness solver protocol" not in prompt
    # Parent state variables are scrubbed; the model credential survives.
    env = captured["env"]
    assert not any(name.startswith("HARNESS_") for name in env)
    assert env["DEEPSEEK_API_KEY"] == "test-key"
    assert env["PI_TELEMETRY"] == "0"
    assert result.text == "FINDINGS: done"
    assert result.timed_out is False


def test_run_subagent_kills_at_timeout(monkeypatch, tmp_path: Path) -> None:
    from harness.pi_host import run_subagent

    monkeypatch.setattr("harness.pi_host.shutil.which", lambda _: "/usr/bin/pi")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    killed: list = []

    class Process:
        returncode = -9
        pid = -2

        def communicate(self, timeout=None):
            if not killed:
                raise subprocess.TimeoutExpired(cmd="pi", timeout=timeout)
            return b"", b""

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(
        "harness.pi_host.subprocess.Popen", lambda *a, **k: Process()
    )
    monkeypatch.setattr(
        "harness.pi_host.os.killpg", lambda pid, sig: killed.append((pid, sig))
    )

    result = run_subagent(_hosted_spec(), "stuck task", timeout=10, cwd=tmp_path)

    assert result.timed_out is True
    assert "time limit" in result.text


def test_hosted_pi_writes_vshell_mcp_config_when_mcp_enabled(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path, pi={"mcp": True})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    class Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["argv"] = command
        Path(kwargs["cwd"], "agent.stdout.log").write_text("", encoding="utf-8")
        return Process()

    monkeypatch.setattr("harness.pi_host.subprocess.Popen", fake_popen)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    adapter.run(_context(task, tmp_path))

    mcp_path = tmp_path / "run-host" / "pi-agent" / "mcp.json"
    assert mcp_path.is_file()
    config = json.loads(mcp_path.read_text(encoding="utf-8"))
    server = config["mcpServers"]["vshell"]
    assert server["command"] == "mcp-remote"
    assert server["args"][0].endswith("/sse")
    assert server["exposure"] == "direct"
    # MCP enabled means no --no-mcp flag reaches Pi.
    assert "--no-mcp" not in captured["argv"]


def test_hosted_pi_omits_mcp_config_when_disabled(monkeypatch, tmp_path: Path) -> None:
    adapter = _adapter(monkeypatch, tmp_path, pi={"mcp": False})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    class Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured["argv"] = command
        Path(kwargs["cwd"], "agent.stdout.log").write_text("", encoding="utf-8")
        return Process()

    monkeypatch.setattr("harness.pi_host.subprocess.Popen", fake_popen)
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))

    adapter.run(_context(task, tmp_path))

    assert not (tmp_path / "run-host" / "pi-agent" / "mcp.json").exists()
    assert "--no-mcp" in captured["argv"]
