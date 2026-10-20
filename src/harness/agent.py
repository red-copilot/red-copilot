from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .agent_workspace import (
    prepare_agent_task_view,
    prepare_agent_workspace_for_container,
)
from .budget import UsageMetrics
from .models import AgentSpec, TaskSpec, _is_reserved_credential_env
from .trace import TraceRecorder


class AgentError(RuntimeError):
    pass


# Agent-authored typed solver events (``action.intent``, ``world.observe``, ...).
AGENT_EVENT_FILE = "agent.events.jsonl"
# Competing sessions share one findings file across containers. The host-side
# layout lives in ``harness.runtime.executors``; this is the container contract.
SHARED_FINDINGS_CONTAINER_DIR = "/run/harness-shared"
SHARED_FINDINGS_CONTAINER_FILE = f"{SHARED_FINDINGS_CONTAINER_DIR}/shared-findings.md"


def task_environment(
    task: TaskSpec,
    *,
    task_dir: Path | str,
    run_dir: Path | str,
    environment_project: str | None,
    seed: int,
    network_profile: str,
    knowledge_dirs: Sequence[str] = (),
    shared_findings_file: str | None = None,
) -> dict[str, str]:
    """Canonical ``HARNESS_*`` task contract for the container Pi Agent."""

    run_root = Path(run_dir)
    environment = {
        "HARNESS_TASK_ID": task.id,
        "HARNESS_TASK_DIR": str(task_dir),
        "HARNESS_RUN_DIR": str(run_root),
        "HARNESS_OBJECTIVE": task.objective.description,
        "HARNESS_ENV_PROJECT": environment_project or "",
        "HARNESS_EVENT_FILE": str(run_root / AGENT_EVENT_FILE),
        "HARNESS_WORLD_INBOX": str(run_root / "world.inbox.jsonl"),
        "HARNESS_WORLD_CONTEXT": str(run_root / "world.context.txt"),
        "HARNESS_SUBMISSION_INBOX": str(run_root / "submission.inbox.jsonl"),
        "HARNESS_FEEDBACK_FILE": str(run_root / "agent.feedback.jsonl"),
        "HARNESS_SUMMARY_FILE": str(run_root / "agent.summary.md"),
        "HARNESS_PROGRESS_FILE": str(run_root / "progress.json"),
        "HARNESS_NOTES_DIR": str(run_root / "notes"),
        "HARNESS_KNOWLEDGE_DIRS": ":".join(knowledge_dirs),
        "HARNESS_NETWORK_PROFILE": network_profile,
        "HARNESS_SEED": str(seed),
    }
    if shared_findings_file:
        environment["HARNESS_SHARED_FINDINGS"] = shared_findings_file
    return environment


def plan_knowledge_mounts(
    roots: Sequence[str],
    *,
    container_root: str = "/knowledge",
) -> list[tuple[Path, str]]:
    """Map declared knowledge roots onto read-only container mount points.

    Fail closed: a declared root that is not a readable directory aborts the run
    instead of silently starting an Agent with no knowledge.
    """
    mounts: list[tuple[Path, str]] = []
    used: set[str] = set()
    for index, entry in enumerate(roots):
        path = Path(entry)
        if not path.is_dir():
            raise AgentError(f"knowledge root is not a directory: {entry}")
        name = path.name or f"root-{index}"
        while name in used:
            name = f"{name}-{index}"
        used.add(name)
        mounts.append((path, f"{container_root}/{name}"))
    return mounts


def gateway_environment(gateway_url: str, gateway_token: str) -> dict[str, str]:
    """Environment that routes an Agent's model traffic through the Harness Gateway."""

    return {
        "HARNESS_GATEWAY_URL": gateway_url,
        "HARNESS_GATEWAY_TOKEN": gateway_token,
        "OPENAI_BASE_URL": f"{gateway_url}/v1",
        "OPENAI_API_KEY": gateway_token,
    }


@dataclass(frozen=True)
class ContainerLimits:
    """Docker resource limits shared by every container the Harness launches."""

    pids: int = 256
    memory: str = "2g"
    cpus: str = "2"
    tmpfs: tuple[str, ...] = ("/tmp:rw,nosuid,nodev,size=256m",)


DEFAULT_CONTAINER_LIMITS = ContainerLimits()


def build_container_command(
    *,
    spec: AgentSpec,
    task: TaskSpec,
    task_dir: Path,
    run_dir: Path,
    container_name: str,
    network: str,
    environment_project: str | None,
    seed: int,
    gateway_url: str | None,
    gateway_token: str | None,
    host_gateway: bool,
    image_command: Sequence[str],
    agent_task_dir: Path | None = None,
    shared_findings_dir: Path | None = None,
    extra_mounts: Sequence[tuple[Path, str]] = (),
    workdir: str = "/task",
    limits: ContainerLimits = DEFAULT_CONTAINER_LIMITS,
    reserved_env_names: frozenset[str] | None = None,
    extra_cap_add: Sequence[str] = (),
    extra_env: dict[str, str] | None = None,
    env_passthrough: Sequence[str] = (),
    interactive: bool = False,
) -> list[str]:
    """Build the hardened Docker command for the container Pi Agent."""

    from .network_policy import proxy_environment_overrides, reject_agent_proxy_env

    reserved = frozenset(spec.env) if reserved_env_names is None else reserved_env_names
    if any(_is_reserved_credential_env(key) for key in reserved):
        raise AgentError(
            "container Agent cannot receive reserved credential environment variables"
        )
    reject_agent_proxy_env(spec.network_profile, set(reserved))
    agent_task_dir = agent_task_dir or prepare_agent_task_view(
        task_dir=task_dir,
        run_root=run_dir.parent,
        verifier_entrypoint=task.verification.entrypoint,
    )
    container_uid, container_gid = prepare_agent_workspace_for_container(run_dir)
    command = [
        "docker",
        "create",
        "--rm",
        "--pull=never",
        "--name",
        container_name,
        "--user",
        f"{container_uid}:{container_gid}",
        *hardened_docker_flags(
            pids_limit=limits.pids,
            memory=limits.memory,
            cpus=limits.cpus,
            tmpfs=limits.tmpfs,
        ),
        "--network",
        network,
    ]
    if spec.runtime:
        command.extend(["--runtime", spec.runtime])
    for capability in (*ROOT_AGENT_CAPABILITIES, *extra_cap_add):
        command.extend(["--cap-add", capability])
    if host_gateway:
        command.extend(["--add-host", "host.docker.internal:host-gateway"])
    command.extend(
        [
            "-v",
            f"{agent_task_dir.resolve()}:/task:ro",
            "-v",
            f"{run_dir.resolve()}:/run/harness:rw",
        ]
    )
    for host_path, container_path in extra_mounts:
        mount_source = Path(host_path)
        if mount_source.is_symlink() or not mount_source.is_dir():
            raise AgentError("extra container mount must be a real directory")
        if not container_path.startswith("/"):
            raise AgentError("extra container mount target must be absolute")
        command.extend(["-v", f"{mount_source.resolve()}:{container_path}:rw"])
    shared_findings_file: str | None = None
    if shared_findings_dir is not None:
        shared_root = Path(shared_findings_dir)
        if shared_root.is_symlink() or not shared_root.is_dir():
            raise AgentError("shared findings directory must be a real directory")
        prepare_agent_workspace_for_container(shared_root)
        command.extend(["-v", f"{shared_root.resolve()}:{SHARED_FINDINGS_CONTAINER_DIR}:rw"])
        shared_findings_file = SHARED_FINDINGS_CONTAINER_FILE
    knowledge_mounts = plan_knowledge_mounts(spec.knowledge)
    for host_path, container_path in knowledge_mounts:
        command.extend(["-v", f"{host_path.resolve()}:{container_path}:ro"])
    command.extend(["-w", workdir])

    environment = task_environment(
        task,
        task_dir="/task",
        run_dir="/run/harness",
        environment_project=environment_project,
        seed=seed,
        network_profile=spec.network_profile,
        knowledge_dirs=[container_path for _, container_path in knowledge_mounts],
        shared_findings_file=shared_findings_file,
    )
    if extra_env:
        environment.update(extra_env)
    gateway_enabled = bool(gateway_url and gateway_token)
    if gateway_enabled:
        environment.update(gateway_environment(str(gateway_url), str(gateway_token)))
    environment.update(spec.env)
    if not gateway_enabled:
        for key in env_passthrough:
            if key not in os.environ:
                raise AgentError(f"requested Pi environment variable is missing: {key}")
            environment[key] = os.environ[key]
    for key, value in environment.items():
        command.extend(["-e", f"{key}={value}"])
    command.extend(proxy_environment_overrides(spec.network_profile))
    if interactive:
        command.append("-i")
    command.extend(image_command)
    return command


@dataclass
class AgentResult:
    returncode: int
    timed_out: bool
    budget_exceeded: str | None
    stdout: str
    stderr: str
    metrics: UsageMetrics


@dataclass(frozen=True)
class AgentRunContext:
    """The single typed parameter object for the Pi RPC session path."""

    task: TaskSpec
    task_dir: Path
    run_dir: Path
    environment_project: str | None = None
    environment_network: str | None = None
    seed: int = 0
    agent_task_dir: Path | None = None
    gateway_url: str | None = None
    gateway_token: str | None = None
    gateway_usage_path: Path | str | None = None
    resume_gateway_event_offset: int = 0
    resume_session_id: str | None = None
    cancel_path: Path | None = None
    # Competing sessions share one agent-authored findings directory; it is the
    # only container-writable path besides the session workspace.
    shared_findings_dir: Path | None = None

    @property
    def gateway_enabled(self) -> bool:
        return bool(self.gateway_url and self.gateway_token)


# Root with every capability dropped cannot write a workspace it does not own
# (no CAP_DAC_OVERRIDE), so these two are the Agent-container baseline; declared
# capabilities such as NET_RAW add on top.
ROOT_AGENT_CAPABILITIES = ("DAC_OVERRIDE", "FOWNER")


def hardened_docker_flags(
    *,
    pids_limit: int,
    memory: str,
    cpus: str,
    tmpfs: Sequence[str] = (),
) -> list[str]:
    """Shared hardening prefix for every container the Harness launches."""

    flags = [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        cpus,
    ]
    for mount in tmpfs:
        flags.extend(["--tmpfs", mount])
    return flags


def build_agent_adapter(spec: AgentSpec, *, trace: TraceRecorder):
    from .pi_container import ContainerPiAdapter

    return ContainerPiAdapter(spec, trace=trace)
