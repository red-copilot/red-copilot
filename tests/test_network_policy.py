from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from harness.agent import AgentError
from harness.models import AgentSpec
from harness.network_policy import EnforcedNetworks, enforced_network


def _network_id(name: str) -> str:
    return "b" * 64 if name == "task_internal" else "a" * 64


def _spec(profile: str) -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "agent",
            "image": "pi:test",
            "network_profile": profile,
            "pi": {"model": "model"},
        }
    )


def test_target_only_requires_and_uses_internal_environment_network(monkeypatch) -> None:
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{_network_id(command[-1])} true\n", stderr=""
        )

    monkeypatch.setattr("harness.network_policy.subprocess.run", inspect)

    network = enforced_network(
        _spec("target-only"),
        environment_network="task_internal",
        gateway_enabled=False,
    )

    assert network == EnforcedNetworks(
        primary=_network_id("task_internal"), environment=_network_id("task_internal")
    )
    assert calls == [
        ["docker", "network", "inspect", "--format", "{{.Id}} {{.Internal}}", "task_internal"]
    ]


def test_target_only_fails_closed_on_non_internal_network(monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.network_policy.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=f"{_network_id(command[-1])} false\n", stderr=""
        ),
    )

    with pytest.raises(AgentError, match="Internal=true"):
        enforced_network(
            _spec("target-only"),
            environment_network="public_bridge",
            gateway_enabled=False,
        )


def test_strict_network_rejects_malformed_inspect_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.network_policy.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="not-a-docker-network-id true\n", stderr=""
        ),
    )

    with pytest.raises(AgentError, match="Internal=true"):
        enforced_network(
            _spec("target-only"),
            environment_network="task_internal",
            gateway_enabled=False,
        )


def test_fully_offline_uses_no_network_and_rejects_gateway() -> None:
    assert (
        enforced_network(
            _spec("fully-offline"),
            environment_network="task_internal",
            gateway_enabled=False,
        )
        == EnforcedNetworks(primary="none")
    )
    with pytest.raises(AgentError, match="incompatible with Gateway"):
        enforced_network(
            _spec("fully-offline"),
            environment_network=None,
            gateway_enabled=True,
        )


def test_legacy_benchmark_only_is_explicitly_advisory() -> None:
    assert (
        enforced_network(
            _spec("benchmark-only"),
            environment_network="public_bridge",
            gateway_enabled=False,
        )
        is None
    )


def test_model_allowed_profile_is_retired() -> None:
    with pytest.raises(ValidationError):
        _spec("model-allowed")
