import os
from pathlib import Path

import pytest

from harness.agent import AgentError, DockerAdapter
from harness.models import AgentSpec, TaskSpec
from harness.pi_container import ContainerPiAdapter
from harness.trace import TraceRecorder
from harness.verifier import build_docker_verifier_command

SHARED_TASK_ENV = (
    "HARNESS_TASK_DIR=/task",
    "HARNESS_RUN_DIR=/run/harness",
    "HARNESS_WORLD_CONTEXT=/run/harness/world.context.txt",
    "HARNESS_SUBMISSION_INBOX=/run/harness/submission.inbox.jsonl",
    "HARNESS_FEEDBACK_FILE=/run/harness/agent.feedback.jsonl",
)
REMOVED_TASK_ENV = (
    "HARNESS_EVENT_FILE=",
    "HARNESS_WORLD_INBOX=",
    "HARNESS_PROGRESS_FILE=",
)


def test_docker_agent_runtime_flag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.agent.shutil.which", lambda _: "/usr/bin/docker")
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "gvisor-agent",
            "type": "docker",
            "image": "agent:test",
            "runtime": "runsc",
        }
    )
    adapter = DockerAdapter(
        spec,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run_test", "task_test"),
    )
    task = TaskSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "task",
            "name": "Task",
            "objective": {"description": "test"},
        }
    )
    protected_run_dir = tmp_path / "run"
    protected_run_dir.mkdir()
    workspace = protected_run_dir / "agent-workspace"
    workspace.mkdir()
    command = adapter._create_command(
        task,
        task_dir=tmp_path,
        run_dir=workspace,
        container_name="agent",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )
    assert command[command.index("--runtime") + 1] == "runsc"
    # The Agent container runs as root; the workspace stays harness-owned and
    # world-readable so a non-root harness can read the artifacts back.
    assert command[command.index("--user") + 1] == "0:0"
    assert workspace.stat().st_uid == os.getuid()
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert command[command.index("--pids-limit") + 1] == "256"
    assert command[command.index("--memory") + 1] == "2g"
    assert command[command.index("--cpus") + 1] == "2"
    assert "/tmp:rw,nosuid,nodev,size=256m" in command
    assert f"{workspace.resolve()}:/run/harness:rw" in command
    assert f"{protected_run_dir.resolve()}:/run/harness:rw" not in command
    task_mount = next(value for value in command if value.endswith(":/task:ro"))
    assert task_mount != f"{tmp_path.resolve()}:/task:ro"
    assert not (protected_run_dir / "agent-task-input" / "verifier.py").exists()
    assert not (protected_run_dir / "agent-task-input" / "agent-workspace").exists()


def test_strict_docker_profile_clears_docker_cli_proxy_defaults(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("harness.agent.shutil.which", lambda _name: "/usr/bin/docker")
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "strict-agent",
            "type": "docker",
            "image": "agent:test",
            "network_profile": "target-only",
        }
    )
    adapter = DockerAdapter(
        spec, trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task")
    )
    task = TaskSpec.model_validate(
        {"apiVersion": "harness/v1", "id": "task", "name": "Task", "objective": {"description": "test"}}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    command = adapter._create_command(
        task,
        task_dir=Path("benchmarks/examples/hello").resolve(),
        run_dir=run_dir,
        container_name="strict-agent",
        network="internal-task",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )

    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "FTP_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "ftp_proxy", "all_proxy",
    ):
        assert f"{name}=" in command


def test_strict_docker_profile_rejects_agent_configured_proxy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.agent.shutil.which", lambda _name: "/usr/bin/docker")
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "strict-agent",
            "type": "docker",
            "image": "agent:test",
            "network_profile": "model-allowed",
            "env": {"HTTPS_PROXY": "http://proxy.invalid"},
        }
    )
    adapter = DockerAdapter(
        spec, trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task")
    )
    task = TaskSpec.model_validate(
        {"apiVersion": "harness/v1", "id": "task", "name": "Task", "objective": {"description": "test"}}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(AgentError, match="strict network profile.*proxy"):
        adapter._create_command(
            task,
            task_dir=Path("benchmarks/examples/hello").resolve(),
            run_dir=run_dir,
            container_name="strict-agent",
            network="gateway-internal",
            environment_project=None,
            seed=1,
            gateway_url="http://harness-gateway:8765",
            gateway_token="gateway-token",
            host_gateway=False,
        )


def test_docker_launch_rechecks_reserved_environment_names(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("harness.agent.shutil.which", lambda _: "/usr/bin/docker")
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1", "id": "container", "type": "docker",
            "image": "agent:test",
        }
    )
    spec.env["HARNESS_CONTROL_TOKEN"] = "secret"
    adapter = DockerAdapter(
        spec, trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task")
    )
    task = TaskSpec.model_validate(
        {"apiVersion": "harness/v1", "id": "task", "name": "Task", "objective": {"description": "test"}}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(AgentError, match="reserved credential environment"):
        adapter._create_command(
            task,
            task_dir=tmp_path,
            run_dir=run_dir,
            container_name="agent",
            network="none",
            environment_project=None,
            seed=1,
            gateway_url=None,
            gateway_token=None,
            host_gateway=False,
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


def test_network_projection_reports_strict_profiles() -> None:
    from harness.runtime.projections import network_result

    target = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "target",
            "type": "docker",
            "image": "agent:test",
            "network_profile": "target-only",
        }
    )
    model = target.model_copy(update={"network_profile": "model-allowed"})
    offline = target.model_copy(update={"network_profile": "fully-offline"})

    assert network_result(target)["enforcement"] == "docker-internal-network"
    assert network_result(model)["enforcement"] == "docker-internal-network-gateway-proxy"
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


def _docker_container_command(monkeypatch, tmp_path: Path) -> list[str]:
    monkeypatch.setattr("harness.agent.shutil.which", lambda _: "/usr/bin/docker")
    adapter = DockerAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "container-agent",
                "type": "docker",
                "image": "agent:test",
            }
        ),
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run", "task"),
    )
    task_dir = Path("benchmarks/examples/hello").resolve()
    run_dir = tmp_path / "run-docker"
    run_dir.mkdir()
    return adapter._create_command(
        _task(),
        task_dir=task_dir,
        run_dir=run_dir,
        container_name="container-agent",
        network="none",
        environment_project=None,
        seed=1,
        gateway_url=None,
        gateway_token=None,
        host_gateway=False,
    )


def _pi_container_command(monkeypatch, tmp_path: Path) -> list[str]:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "secret")
    monkeypatch.setattr("harness.pi_container.shutil.which", lambda _: "/usr/bin/docker")
    adapter = ContainerPiAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-agent",
                "type": "pi",
                "image": "harness-pi-kali:test",
                "pi": {"provider": "fake", "model": "m", "mode": "json"},
            }
        ),
        allow_host=False,
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


@pytest.mark.parametrize("builder", [_docker_container_command, _pi_container_command])
def test_container_agents_share_hardening_flags(monkeypatch, tmp_path: Path, builder) -> None:
    command = builder(monkeypatch, tmp_path)

    assert "--privileged" not in command
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert command[command.index("--network") + 1] == "none"
    workspace = tmp_path / ("run-docker" if builder is _docker_container_command else "run-pi")
    assert command[command.index("--user") + 1] == "0:0"
    assert workspace.stat().st_uid == os.getuid()
    for entry in SHARED_TASK_ENV:
        assert entry in command, entry
    for entry in REMOVED_TASK_ENV:
        assert not any(value.startswith(entry) for value in command), entry
    assert "/tmp:rw,nosuid,nodev" in " ".join(command)


def test_container_agents_keep_their_own_resource_limits(monkeypatch, tmp_path: Path) -> None:
    docker_command = _docker_container_command(monkeypatch, tmp_path)
    pi_command = _pi_container_command(monkeypatch, tmp_path)

    assert docker_command[docker_command.index("--pids-limit") + 1] == "256"
    assert docker_command[docker_command.index("--memory") + 1] == "2g"
    assert docker_command[docker_command.index("--cpus") + 1] == "2"

    assert pi_command[pi_command.index("--pids-limit") + 1] == "512"
    assert pi_command[pi_command.index("--memory") + 1] == "4g"
    assert pi_command[pi_command.index("--cpus") + 1] == "4"
    assert "/var/tmp:rw,nosuid,nodev,size=256m" in pi_command


def _knowledge_adapter(tmp_path: Path, knowledge: list[str]) -> ContainerPiAdapter:
    return ContainerPiAdapter(
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "pi-knowledge",
                "type": "pi",
                "image": "harness-pi-kali:test",
                "knowledge": knowledge,
                "pi": {"provider": "fake", "model": "m", "mode": "json"},
            }
        ),
        allow_host=False,
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
