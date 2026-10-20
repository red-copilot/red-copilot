from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.models import AgentSpec, TaskSpec, load_agent, load_task


def test_example_contracts_are_valid() -> None:
    task = load_task(Path("benchmarks/examples/hello/task.yaml"))
    docker_verifier = load_task(
        Path("benchmarks/examples/hello-docker-verifier/task.yaml")
    )
    pi_agent = load_agent(Path("agents/examples/pi.yaml"))
    hackathon_agent = load_agent(Path("agents/examples/pi-tsec-hackathon.yaml"))
    assert task.api_version == "harness/v1"
    assert task.budgets.wall_time == 30
    assert docker_verifier.verification.type == "docker"
    assert docker_verifier.verification.network == "none"
    assert pi_agent.image == "harness/pi-kali:local"
    assert pi_agent.pi is not None
    assert pi_agent.pi.model == "gpt-5.6-sol"
    assert pi_agent.pi.cap_add == ["NET_RAW"]
    assert hackathon_agent.pi is not None
    assert hackathon_agent.pi.system_prompt_file == "prompts/tsec-solver.md"


def test_competition_agents_opt_into_the_harness_context_extension() -> None:
    # RUN-06: the tool_call clamp and the tool_result spill are opt-in, and only
    # the competition profiles opt in; every other config keeps the default.
    for name in ("pi-tsec-opencode.yaml", "zc-offensive.yaml"):
        agent = load_agent(Path("agents/examples") / name)
        assert agent.pi is not None
        assert agent.pi.harness_extension is True, name

    for name in ("pi.yaml", "pi-tsec.yaml", "pi-tsec-hackathon.yaml"):
        path = Path("agents/examples") / name
        agent = load_agent(path)
        assert agent.pi is not None
        assert agent.pi.harness_extension is False, name


def test_pi_agent_requires_pi_config_and_image() -> None:
    with pytest.raises(ValidationError, match="pi configuration"):
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "bad-pi",
                "image": "pi:test",
            }
        )
    with pytest.raises(ValidationError, match="container image"):
        AgentSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "bad-pi-image",
                "pi": {"model": "m"},
            }
        )


def test_pi_accepts_container_runtime() -> None:
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "pi-runsc",
            "image": "pi:test",
            "runtime": "runsc",
            "pi": {"model": "m"},
        }
    )
    assert spec.runtime == "runsc"
    assert spec.pi is not None
    assert spec.pi.cap_add == []


@pytest.mark.parametrize(
    "agent_data",
    [
        {
            "apiVersion": "harness/v1",
            "id": "legacy-type",
            "type": "pi",
            "image": "pi:test",
            "pi": {"model": "m"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-command",
            "image": "pi:test",
            "command": ["echo"],
            "pi": {"model": "m"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-cwd",
            "image": "pi:test",
            "cwd": "/tmp",
            "pi": {"model": "m"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-mode",
            "image": "pi:test",
            "pi": {"model": "m", "mode": "json"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-hosted-model",
            "image": "pi:test",
            "pi": {
                "model": "m",
                "models_base_url": "https://example.invalid",
                "models_api_key_env": "MODEL_KEY",
            },
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-cli",
            "type": "cli",
            "command": ["echo"],
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-docker",
            "type": "docker",
            "image": "agent:test",
        },
        {
            "apiVersion": "harness/v1",
            "id": "legacy-pi-host",
            "type": "pi-host",
            "pi": {"model": "m"},
        },
    ],
)
def test_retired_agent_contract_fields_are_rejected(agent_data) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentSpec.model_validate(agent_data)


@pytest.mark.parametrize(
    "agent_data",
    [
        {
            "apiVersion": "harness/v1",
            "id": "container-agent",
            "image": "agent:test",
            "env": {"BENCHMARK_TOKEN": "leak"},
            "pi": {"model": "m"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "pi-agent",
            "image": "pi:test",
            "pi": {"model": "m", "env_passthrough": ["BENCHMARK_TOKEN"]},
        },
        {
            "apiVersion": "harness/v1",
            "id": "pi-agent",
            "image": "pi:test",
            "pi": {"model": "m", "env_passthrough": ["HARNESS_CONTROL_TOKEN"]},
        },
        {
            "apiVersion": "harness/v1",
            "id": "pi-agent",
            "image": "pi:test",
            "env": {"HARNESS_MODEL_API_KEY": "leak"},
            "pi": {"model": "m"},
        },
        {
            "apiVersion": "harness/v1",
            "id": "pi-agent",
            "image": "pi:test",
            "pi": {"model": "m", "env_passthrough": ["CUSTOM_SERVICE_TOKEN"]},
        },
    ],
)
def test_container_agents_cannot_passthrough_harness_or_benchmark_credentials(agent_data) -> None:
    with pytest.raises(ValidationError, match="token-named or reserved credential"):
        AgentSpec.model_validate(agent_data)


def test_pi_can_passthrough_explicit_model_provider_api_key() -> None:
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "pi-agent",
            "image": "pi:test",
            "pi": {"model": "m", "env_passthrough": ["OPENAI_API_KEY"]},
        }
    )

    assert spec.pi is not None
    assert spec.pi.env_passthrough == ["OPENAI_API_KEY"]


def test_docker_verifier_requires_image() -> None:
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "apiVersion": "harness/v1",
                "id": "bad-verifier",
                "name": "Bad verifier",
                "objective": {"description": "test"},
                "verification": {"type": "docker"},
            }
        )


def _pi_agent(**overrides) -> AgentSpec:
    payload = {
        "apiVersion": "harness/v1",
        "id": "pi-agent",
        "image": "pi:test",
        "pi": {"provider": "openai", "model": "base-model", "thinking": "low"},
    }
    payload.update(overrides)
    return AgentSpec.model_validate(payload)


def test_agent_without_executors_is_its_own_single_session() -> None:
    agent = _pi_agent()

    assert agent.with_executors() == [agent]


def test_executors_derive_one_validated_session_each() -> None:
    agent = _pi_agent(
        executors=[
            {"id": "fast", "model": "cheap-model", "thinking": "minimal"},
            {"id": "deep", "model": "strong-model", "extra_args": ["--temperature", "0.9"]},
        ]
    )

    sessions = agent.with_executors()

    assert [session.id for session in sessions] == ["pi-agent#1-fast", "pi-agent#2-deep"]
    assert [session.pi.model for session in sessions] == ["cheap-model", "strong-model"]
    assert sessions[0].pi.thinking == "minimal"
    assert sessions[1].pi.thinking == "low"  # inherited from the base agent
    assert sessions[1].pi.extra_args == ["--temperature", "0.9"]
    assert all(session.executors == [] for session in sessions)


def test_executor_overrides_and_fields_are_validated_at_load_time() -> None:
    with pytest.raises(ValidationError):
        _pi_agent(executors=[{"id": "dup"}, {"id": "dup"}])
    with pytest.raises(ValidationError):
        _pi_agent(executors=[{"id": "blank-model", "model": ""}])
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _pi_agent(executors=[{"id": "legacy", "type": "pi"}])
