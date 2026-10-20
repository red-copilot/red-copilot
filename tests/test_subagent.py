from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from harness.models import AgentSpec, load_task
from harness.pi_container import ContainerPiAdapter
from harness.trace import TraceRecorder


def _spec(*, mcp: bool = False) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "subagent",
            "image": "pi:test",
            "network": "none",
            "network_profile": "fully-offline",
            "pi": {"model": "fake-model", "mcp": mcp},
        }
    )


def test_subagent_rpc_command_mounts_cwd_and_hides_parent_protocol(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    workdir = tmp_path / "work"
    workdir.mkdir()
    scratch = tmp_path / "scratch"
    run_dir = scratch / "state" / "run"
    task_dir = scratch / "task"
    run_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)
    (task_dir / "verifier.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    adapter = ContainerPiAdapter(
        _spec(),
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task"),
        include_solver_protocol=False,
        extra_mounts=[(workdir, "/workspace")],
        workdir="/workspace",
    )

    command = adapter._container_command(
        task,
        task_dir=task_dir,
        run_dir=run_dir,
        container_name="subagent",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )

    assert f"{workdir.resolve()}:/workspace:rw" in command
    assert command[command.index("-w") + 1] == "/workspace"
    assert command[command.index("--mode") + 1] == "rpc"
    prompt = adapter._prompt_text(task)
    assert task.objective.description in prompt
    assert "Harness solver protocol" not in prompt


def test_assistant_text_extracts_final_rpc_message() -> None:
    from harness.subagent import _assistant_text

    raw = "\n".join(
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
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "name": "bash"},
                            {"type": "text", "text": "FINDINGS: done"},
                        ],
                    },
                }
            ),
            "not json",
        ]
    )

    assert _assistant_text(raw) == "first reply\n\nFINDINGS: done"


def test_run_subagent_returns_unverified_container_text(monkeypatch, tmp_path: Path) -> None:
    import harness.subagent as subagent_module

    captured: dict = {}

    async def fake_run(*, spec, objective, timeout, workdir, scratch):
        captured.update(
            spec=spec,
            objective=objective,
            timeout=timeout,
            workdir=workdir,
            scratch=scratch,
        )
        captured["raw"] = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "FINDINGS: offset 136"}],
                },
            }
        )
        return SimpleNamespace(returncode=0, timed_out=False)

    monkeypatch.setattr(subagent_module, "_run_rpc_subagent", fake_run)
    monkeypatch.setattr(
        subagent_module,
        "_read_subagent_stdout",
        lambda _scratch: captured["raw"],
    )

    result = subagent_module.run_subagent(
        _spec(), "reverse the binary", timeout=60, cwd=tmp_path
    )

    assert result.text == "FINDINGS: offset 136"
    assert result.returncode == 0
    assert result.timed_out is False
    assert captured["objective"] == "reverse the binary"
    assert captured["timeout"] == 60
    assert captured["workdir"] == tmp_path.resolve()


def test_run_subagent_marks_timeout_and_keeps_partial_text(monkeypatch, tmp_path: Path) -> None:
    import harness.subagent as subagent_module

    async def fake_run(**_kwargs):
        return SimpleNamespace(returncode=-9, timed_out=True)

    monkeypatch.setattr(subagent_module, "_run_rpc_subagent", fake_run)
    monkeypatch.setattr(
        subagent_module,
        "_read_subagent_stdout",
        lambda _scratch: json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial"}],
                },
            }
        ),
    )

    result = subagent_module.run_subagent(_spec(), "stuck task", timeout=10, cwd=tmp_path)

    assert result.timed_out is True
    assert result.returncode == -9
    assert "partial" in result.text
    assert "time limit" in result.text
