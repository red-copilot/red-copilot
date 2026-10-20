from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _is_reserved_credential_env(name: str) -> bool:
    normalized = name.upper()
    credential_marker = any(
        marker in normalized
        for marker in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    )
    return "TOKEN" in normalized or (credential_marker and (
        normalized.startswith(("HARNESS_", "TSEC_"))
        or "BENCHMARK" in normalized
    ))


class ObjectiveSpec(BaseModel):
    description: str = Field(min_length=1)


class BudgetSpec(BaseModel):
    wall_time: int = Field(default=3600, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class EnvironmentSpec(BaseModel):
    provider: Literal["none", "docker-compose"] = "none"
    manifest: str | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> EnvironmentSpec:
        if self.provider == "docker-compose" and not self.manifest:
            raise ValueError("docker-compose environments require manifest")
        return self


class VerificationSpec(BaseModel):
    type: Literal["python", "docker"] = "python"
    entrypoint: str = "verifier.py"
    image: str | None = None
    command: list[str] = Field(default_factory=list)
    network: Literal["none", "environment"] = "none"
    timeout: int = Field(default=120, ge=1, le=3600)
    runtime: str | None = None

    @model_validator(mode="after")
    def validate_verifier(self) -> VerificationSpec:
        if self.type == "docker" and not self.image:
            raise ValueError("docker verifier requires image")
        if self.type == "python" and self.runtime is not None:
            raise ValueError("runtime is only valid for docker verifier")
        return self


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class TaskSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_version: Literal["harness/v1"] = Field(alias="apiVersion")
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = "general"
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)
    objective: ObjectiveSpec
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    budgets: BudgetSpec = Field(default_factory=BudgetSpec)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)


class PiSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binary: str = Field(default="pi", min_length=1)
    launcher_args: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str = Field(min_length=1)
    thinking: ThinkingLevel | None = None
    tools: list[str] = Field(default_factory=lambda: ["read", "bash", "edit", "write"])
    approve_project: bool = False
    context_files: bool = False
    extensions: bool = False
    harness_extension: bool = False
    skills: bool = False
    prompt_templates: bool = False
    themes: bool = False
    mcp: bool = False
    offline: bool = True
    system_prompt_file: str | None = None
    env_passthrough: list[str] = Field(default_factory=list)
    cap_add: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)


class ExecutorSpec(BaseModel):
    """One competing solver session for a single provisioned challenge.

    Only the session-level overrides are declared here. The image, network and
    tool set stay on ``AgentSpec``: changing those changes the execution
    contract itself, not just how one competing Pi session is configured.
    Everything omitted is inherited from the agent's base config.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = Field(default=None, min_length=1)
    thinking: ThinkingLevel | None = None
    system_prompt_file: str | None = None
    extra_args: list[str] = Field(default_factory=list)


# Competing sessions share one provisioned challenge and each one runs in its
# own Agent container, so the ceiling keeps ``challenges x executors`` inside
# the machine budget the run is planned for.
MAX_EXECUTOR_SESSIONS = 4


class AgentSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    api_version: Literal["harness/v1"] = Field(default="harness/v1", alias="apiVersion")
    id: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    image: str | None = None
    network: Literal["environment", "none", "host"] = "environment"
    network_profile: Literal[
        "offline",
        "benchmark-only",
        "unrestricted",
        "target-only",
        "fully-offline",
    ] = "unrestricted"
    runtime: str | None = None
    pi: PiSpec | None = None
    # Read-only markdown knowledge roots (payload catalogues, cheat sheets,
    # SKILL.md skill files) exposed to the Agent's file tools. The Agent reads
    # them on demand; the Harness never parses or plans over them.
    knowledge: list[str] = Field(default_factory=list)
    # Competing solver sessions for one provisioned challenge. Empty means a
    # single session on this agent's own configuration.
    executors: list[ExecutorSpec] = Field(default_factory=list)

    def with_executors(self) -> list[AgentSpec]:
        """Derive one validated AgentSpec per competing session.

        Re-validated rather than shallow-copied so a bad executor override fails
        at load time instead of inside a run.
        """
        if not self.executors:
            return [self]
        payload = self.model_dump(by_alias=True)
        derived: list[AgentSpec] = []
        for index, executor in enumerate(self.executors):
            session = dict(payload)
            session["id"] = f"{self.id}#{index + 1}-{executor.id}"
            session["executors"] = []
            pi_payload = dict(session.get("pi") or {})
            if pi_payload:
                if executor.provider is not None:
                    pi_payload["provider"] = executor.provider
                if executor.model is not None:
                    pi_payload["model"] = executor.model
                if executor.thinking is not None:
                    pi_payload["thinking"] = executor.thinking
                if executor.system_prompt_file is not None:
                    pi_payload["system_prompt_file"] = executor.system_prompt_file
                if executor.extra_args:
                    pi_payload["extra_args"] = [
                        *pi_payload.get("extra_args", []),
                        *executor.extra_args,
                    ]
                session["pi"] = pi_payload
            derived.append(AgentSpec.model_validate(session))
        return derived

    @model_validator(mode="after")
    def validate_executors(self) -> AgentSpec:
        if not self.executors:
            return self
        if len(self.executors) > MAX_EXECUTOR_SESSIONS:
            raise ValueError(
                f"at most {MAX_EXECUTOR_SESSIONS} competing executors are supported per case"
            )
        ids = [executor.id for executor in self.executors]
        if len(set(ids)) != len(ids):
            raise ValueError("executor ids must be unique")
        self.with_executors()  # fail fast on any invalid session override
        return self

    @model_validator(mode="after")
    def validate_adapter(self) -> AgentSpec:
        if self.pi is None:
            raise ValueError("Pi agents require pi configuration")
        if not self.image:
            raise ValueError("Pi agents require a container image")
        passthrough = set(self.env)
        passthrough.update(self.pi.env_passthrough)
        forbidden = sorted(name for name in passthrough if _is_reserved_credential_env(name))
        if forbidden:
            raise ValueError(
                "container Agent cannot receive token-named or reserved credential "
                f"environment variables: {', '.join(forbidden)}"
            )
        return self


class VerificationResult(BaseModel):
    success: bool
    score: float = Field(ge=0, le=100)
    message: str | None = None
    milestones: dict[str, bool] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"{path} must contain a YAML object")
    return data


def load_task(path: str | Path) -> TaskSpec:
    return TaskSpec.model_validate(_load_yaml(Path(path)))


def load_agent(path: str | Path) -> AgentSpec:
    return AgentSpec.model_validate(_load_yaml(Path(path)))
