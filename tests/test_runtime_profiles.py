import os
from pathlib import Path

import pytest

from harness.agent import AgentError
from harness.models import AgentSpec, TaskSpec
from harness.pi_container import ContainerPiAdapter
from harness.trace import TraceRecorder
from harness.verifier import build_docker_verifier_command

SHARED_TASK_ENV = (
    "HARNESS_TASK_DIR=/task",
    "HARNESS_RUN_DIR=/run/harness",
    "HARNESS_EVENT_FILE=/run/harness/agent.events.jsonl",
    "HARNESS_WORLD_INBOX=/run/harness/world.inbox.jsonl",
    "HARNESS_WORLD_CONTEXT=/run/harness/world.context.txt",
    "HARNESS_SUBMISSION_INBOX=/run/harness/submission.inbox.jsonl",
    "HARNESS_FEEDBACK_FILE=/run/harness/agent.feedback.jsonl",
    "HARNESS_PROGRESS_FILE=/run/harness/progress.json",
)


def test_docker_verifier_runtime_flag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.verifier.shutil.which", lambda _: "/usr/bin/docker")
    task = TaskSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "task",
            "name": "Task",
            "objective": {"description": "test"},
            "verification": {
                "type": "docker",
                "image": "verifier:test",
                "runtime": "runsc",
            },
        }
    )
    command = build_docker_verifier_command(
        task,
        task_dir=tmp_path,
        run_dir=tmp_path,
        environment_project=None,
        environment_network=None,
        seed=1,
    )
    assert command[command.index("--runtime") + 1] == "runsc"
    assert command[command.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_network_projection_reports_strict_profiles() -> None:
    from harness.runtime.projections import network_result

    target = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "target",
            "image": "pi:test",
            "network_profile": "target-only",
            "pi": {"model": "m"},
        }
    )
    offline = target.model_copy(update={"network_profile": "fully-offline"})

    assert network_result(target)["enforcement"] == "docker-internal-network"
    assert network_result(offline)["enforcement"] == "docker-none"


def _task() -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "task",
            "name": "Task",
            "objective": {"description": "test"},
        }
    )


def _pi_container_command(monkeypatch, tmp_path: Path) -> list[str]:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    adapter = ContainerPiAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-agent",
                                "image": "harness-pi-kali:test",
                "pi": {"provider": "fake", "model": "m"},
            }
        ),
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task"),
    )
    task_dir = Path("benchmarks/examples/hello").resolve()
    run_dir = tmp_path / "run-pi"
    run_dir.mkdir()
    return adapter._container_command(
        _task(),
        task_dir=task_dir,
        run_dir=run_dir,
        container_name="pi-agent",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )


def test_pi_container_hardening_and_resource_limits(monkeypatch, tmp_path: Path) -> None:
    command = _pi_container_command(monkeypatch, tmp_path)

    assert "--privileged" not in command
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert command[command.index("--network") + 1] == "none"
    workspace = tmp_path / "run-pi"
    assert command[command.index("--user") + 1] == "0:0"
    assert workspace.stat().st_uid == os.getuid()
    for entry in SHARED_TASK_ENV:
        assert entry in command, entry
    assert "/tmp:rw,nosuid,nodev,size=512m" in command
    assert command[command.index("--pids-limit") + 1] == "512"
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "4"
    assert "/var/tmp:rw,nosuid,nodev,size=256m" in command


def _knowledge_adapter(tmp_path: Path, knowledge: list[str]) -> ContainerPiAdapter:
    return ContainerPiAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-knowledge",
                                "image": "harness-pi-kali:test",
                "knowledge": knowledge,
                "pi": {"provider": "fake", "model": "m"},
            }
        ),
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task"),
    )


def test_knowledge_roots_are_mounted_read_only_and_exported(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    catalog = tmp_path / "payloads"
    catalog.mkdir()
    (catalog / "sqli.md").write_text("# payloads\n", encoding="utf-8")
    adapter = _knowledge_adapter(tmp_path, [str(catalog)])
    run_dir = tmp_path / "run-knowledge"
    run_dir.mkdir()

    command = adapter._container_command(
        _task(),
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="pi-knowledge",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )

    # Read-only mount: the Agent reads the catalogue but cannot rewrite it.
    assert f"{catalog.resolve()}:/knowledge/payloads:ro" in command
    assert "HARNESS_KNOWLEDGE_DIRS=/knowledge/payloads" in command
    assert "HARNESS_NOTES_DIR=/run/harness/notes" in command
    assert any(str(entry).endswith("/run/harness:rw") for entry in command)


def test_missing_knowledge_root_fails_closed(tmp_path: Path) -> None:
    adapter = _knowledge_adapter(tmp_path, [str(tmp_path / "absent")])
    run_dir = tmp_path / "run-missing"
    run_dir.mkdir()

    with pytest.raises(AgentError):
        adapter._container_command(
            _task(),
            task_dir=Path("benchmarks/examples/hello").resolve(),
            run_dir=run_dir,
            container_name="pi-knowledge",
            network="none",
            environment_project=None,
            seed=1,
            gateway_url=None,
            gateway_token=None,
            host_gateway=False,
        )
