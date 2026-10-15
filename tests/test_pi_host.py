from __future__ import annotations

import json
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
