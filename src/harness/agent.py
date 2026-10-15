from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .agent_workspace import (
    prepare_agent_task_view,
    prepare_agent_workspace_for_container,
)
from .budget import FINALIZE_GRACE_SECONDS, BudgetMonitor, UsageMetrics
from .models import AgentSpec, TaskSpec, _is_reserved_credential_env
from .secureio import open_regular_file, read_regular_text
from .trace import TraceRecorder


class AgentError(RuntimeError):
    pass


# Harness-normalized Agent event stream (the per-run artifact).
NORMALIZED_EVENT_FILE = "events.jsonl"
# Competing sessions share one findings file across containers. The host-side
# layout lives in ``harness.runtime.executors``; this is the container contract.
SHARED_FINDINGS_CONTAINER_DIR = "/run/harness-shared"
SHARED_FINDINGS_CONTAINER_FILE = f"{SHARED_FINDINGS_CONTAINER_DIR}/shared-findings.md"
# Agent-authored typed solver events (``action.intent``, ``world.observe``, ...).
# RPC sessions read Pi's stdout rather than the shared file, so the Agent gets
# its own file there; one-shot JSON sessions keep the shared file.
AGENT_EVENT_FILE = "agent.events.jsonl"


def task_environment(
    task: TaskSpec,
    *,
    task_dir: Path | str,
    run_dir: Path | str,
    environment_project: str | None,
    seed: int,
    network_profile: str,
    event_file_name: str = NORMALIZED_EVENT_FILE,
    knowledge_dirs: Sequence[str] = (),
    shared_findings_file: str | None = None,
) -> dict[str, str]:
    """Canonical ``HARNESS_*`` task contract for host and container Agents."""

    run_root = Path(run_dir)
    environment = {
        "HARNESS_TASK_ID": task.id,
        "HARNESS_TASK_DIR": str(task_dir),
        "HARNESS_RUN_DIR": str(run_root),
        "HARNESS_OBJECTIVE": task.objective.description,
        "HARNESS_ENV_PROJECT": environment_project or "",
        "HARNESS_EVENT_FILE": str(run_root / event_file_name),
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


def host_knowledge_dirs(spec: AgentSpec) -> list[str]:
    """Host-visible knowledge roots for trusted non-container Agents."""
    return [str(path.resolve()) for path, _ in plan_knowledge_mounts(spec.knowledge)]


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
    """Docker resource limits shared by container Agent adapters."""

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
    workdir: str = "/task",
    limits: ContainerLimits = DEFAULT_CONTAINER_LIMITS,
    reserved_env_names: frozenset[str] | None = None,
    extra_cap_add: Sequence[str] = (),
    extra_env: Mapping[str, str] | None = None,
    env_passthrough: Sequence[str] = (),
    interactive: bool = False,
    event_file_name: str = NORMALIZED_EVENT_FILE,
) -> list[str]:
    """Build the single hardened ``docker create`` command for container Agents."""

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
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        limits.memory,
        "--cpus",
        limits.cpus,
    ]
    for mount in limits.tmpfs:
        command.extend(["--tmpfs", mount])
    command.extend(["--network", network])
    if spec.runtime:
        command.extend(["--runtime", spec.runtime])
    for capability in extra_cap_add:
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
        event_file_name=event_file_name,
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
    """The single typed parameter object for every adapter run/session path.

    Adapters used to take 12+ loose keywords duplicated across four classes;
    one frozen context keeps the call sites and the session constructors from
    drifting apart. ``dataclasses.replace`` builds derived contexts (for
    example filling the one-shot cancel path) without mutation.
    """

    task: TaskSpec
    task_dir: Path
    run_dir: Path
    environment_project: str | None = None
    environment_network: str | None = None
    seed: int = 0
    agent_task_dir: Path | None = None
    gateway_url: str | None = None
    gateway_token: str | None = None
    gateway_network: str | None = None
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


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)


def _run_monitored(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    run_dir: Path,
    task: TaskSpec,
    trace: TraceRecorder,
    cancel_path: Path | None = None,
    guard_parent_death: bool = False,
) -> AgentResult:
    event_file = run_dir / "events.jsonl"
    event_file.write_text("", encoding="utf-8")
    stdout_path = run_dir / "agent.stdout.log"
    stderr_path = run_dir / "agent.stderr.log"
    monitor = BudgetMonitor(event_file, task.budgets, trace)
    timed_out = False
    started = time.monotonic()

    with (
        open_regular_file(stdout_path, "wb") as stdout_handle,
        open_regular_file(stderr_path, "wb") as stderr_handle,
    ):
        launch_command = command
        if guard_parent_death and sys.platform == "linux":
            guard = Path(__file__).with_name("process_guard.py")
            launch_command = [sys.executable, str(guard), "--", *command]
        proc = subprocess.Popen(
            launch_command,
            cwd=cwd,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        while proc.poll() is None:
            if cancel_path is not None:
                try:
                    cancelled = bool(read_regular_text(cancel_path, max_bytes=64 * 1024))
                except FileNotFoundError:
                    cancelled = False
                except OSError:
                    cancelled = True
                if cancelled:
                    trace.emit("agent.cancelled", data={"reason": "session_closed"})
                    _terminate_process(proc)
                    break
            exceeded = monitor.poll()
            if exceeded:
                _terminate_process(proc)
                break
            if time.monotonic() - started > task.budgets.wall_time + FINALIZE_GRACE_SECONDS:
                timed_out = True
                trace.emit(
                    "budget.exceeded",
                    data={
                        "budget": "wall_time",
                        "limit": task.budgets.wall_time,
                        "value": time.monotonic() - started,
                    },
                )
                _terminate_process(proc)
                break
            time.sleep(0.1)

        returncode = proc.wait()

    monitor.finish()
    return AgentResult(
        returncode=returncode,
        timed_out=timed_out,
        budget_exceeded=monitor.exceeded,
        stdout=read_regular_text(stdout_path),
        stderr=read_regular_text(stderr_path),
        metrics=monitor.metrics,
    )


class CLIAdapter:
    """Trusted local-process adapter used for development."""

    def __init__(self, spec: AgentSpec, *, allow_host: bool, trace: TraceRecorder) -> None:
        if not allow_host:
            raise AgentError(
                "CLI agents execute on the host; pass --allow-host-agent only for trusted agents"
            )
        self.spec = spec
        self.trace = trace

    @property
    def capabilities(self):
        from .session import ONE_SHOT_AGENT_CAPABILITIES

        return ONE_SHOT_AGENT_CAPABILITIES

    def run(self, context: AgentRunContext) -> AgentResult:
        # Host Agents remain an explicit, unsandboxed development path and
        # ignore agent_task_dir; they see the task tree directly.
        from .network_policy import enforced_network

        enforced_network(
            self.spec,
            environment_network=context.environment_network,
            gateway_network=context.gateway_network,
            gateway_enabled=context.gateway_enabled,
        )
        env = os.environ.copy()
        env.update(self.spec.env)
        env.update(
            task_environment(
                context.task,
                task_dir=context.task_dir,
                run_dir=context.run_dir,
                environment_project=context.environment_project,
                seed=context.seed,
                network_profile=self.spec.network_profile,
                knowledge_dirs=host_knowledge_dirs(self.spec),
            )
        )
        if context.gateway_enabled:
            env.update(gateway_environment(context.gateway_url, context.gateway_token))

        cwd = Path(self.spec.cwd).resolve() if self.spec.cwd else context.task_dir
        self.trace.emit(
            "agent.started",
            actor="agent",
            data={
                "agent_id": self.spec.id,
                "type": self.spec.type,
                "gateway_injected": context.gateway_url is not None,
            },
        )
        result = _run_monitored(
            self.spec.command,
            cwd=cwd,
            env=env,
            run_dir=context.run_dir,
            task=context.task,
            trace=self.trace,
            cancel_path=context.cancel_path,
            guard_parent_death=True,
        )
        self._finish_trace(result)
        return result

    async def start_session(self, context: AgentRunContext):
        from dataclasses import replace

        from .session import OneShotAgentSession

        context = replace(
            context,
            cancel_path=context.cancel_path or context.run_dir / "agent.control.jsonl",
        )
        return await OneShotAgentSession.start(self, context=context)

    def _finish_trace(self, result: AgentResult) -> None:
        self.trace.emit(
            "agent.finished",
            actor="agent",
            data={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "budget_exceeded": result.budget_exceeded,
                "metrics": result.metrics.as_dict(),
            },
        )


class DockerAdapter:
    """Containerized agent adapter with restrictive defaults and multi-network support."""

    def __init__(self, spec: AgentSpec, *, trace: TraceRecorder) -> None:
        if shutil.which("docker") is None:
            raise AgentError("docker executable was not found")
        self.spec = spec
        self.trace = trace

    @property
    def capabilities(self):
        from .session import ONE_SHOT_AGENT_CAPABILITIES

        return ONE_SHOT_AGENT_CAPABILITIES

    @staticmethod
    def _docker(
        command: list[str],
        *,
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if check and result.returncode != 0:
            raise AgentError(f"{' '.join(command[:3])} failed: {result.stderr.strip()}")
        return result

    def _create_command(
        self,
        task: TaskSpec,
        *,
        task_dir: Path,
        run_dir: Path,
        container_name: str,
        network: str,
        environment_project: str | None,
        seed: int,
        gateway_url: str | None,
        gateway_token: str | None,
        host_gateway: bool,
        agent_task_dir: Path | None = None,
        shared_findings_dir: Path | None = None,
    ) -> list[str]:
        return build_container_command(
            spec=self.spec,
            task=task,
            task_dir=task_dir,
            run_dir=run_dir,
            container_name=container_name,
            network=network,
            environment_project=environment_project,
            seed=seed,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            host_gateway=host_gateway,
            agent_task_dir=agent_task_dir,
            shared_findings_dir=shared_findings_dir,
            image_command=[str(self.spec.image), *self.spec.command],
        )

    def run(self, context: AgentRunContext) -> AgentResult:
        from .network_policy import enforced_network

        task = context.task
        task_dir = context.task_dir
        run_dir = context.run_dir
        gateway_url = context.gateway_url
        gateway_token = context.gateway_token
        gateway_network = context.gateway_network
        environment_network = context.environment_network
        gateway_enabled = context.gateway_enabled
        enforced = enforced_network(
            self.spec,
            environment_network=environment_network,
            gateway_network=gateway_network,
            gateway_enabled=gateway_enabled,
        )
        if gateway_enabled and (
            self.spec.network == "none" or self.spec.network_profile == "offline"
        ):
            raise AgentError("Docker Agent offline networking is incompatible with per-run Gateway")

        container_name = ("rh_agent_" + run_dir.name.lower()).replace("-", "_")[:63]
        sidecar_gateway = gateway_enabled and gateway_network is not None
        host_gateway = gateway_enabled and not sidecar_gateway and self.spec.network != "host"

        if enforced is not None:
            primary_network = enforced.primary
        elif self.spec.network_profile == "offline":
            primary_network = "none"
        elif sidecar_gateway:
            if self.spec.network == "host":
                raise AgentError("Docker Agent network:host is incompatible with sidecar Gateway")
            primary_network = str(gateway_network)
        elif self.spec.network == "host":
            primary_network = "host"
        elif self.spec.network == "environment":
            primary_network = environment_network or ("bridge" if host_gateway else "none")
        else:
            primary_network = "none"

        create_command = self._create_command(
            task,
            task_dir=task_dir,
            run_dir=run_dir,
            container_name=container_name,
            network=primary_network,
            environment_project=context.environment_project,
            seed=context.seed,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            host_gateway=host_gateway,
            agent_task_dir=context.agent_task_dir,
            shared_findings_dir=context.shared_findings_dir,
        )

        self.trace.emit(
            "agent.started",
            actor="agent",
            data={
                "agent_id": self.spec.id,
                "type": self.spec.type,
                "image": self.spec.image,
                "network": primary_network,
                "target_network": environment_network,
                "runtime": self.spec.runtime,
                "gateway_injected": gateway_enabled,
                "gateway_network": gateway_network,
            },
        )

        try:
            self._docker(create_command, cwd=task_dir)
            enforced_target = enforced.environment if enforced is not None else None
            target_differs = (
                enforced_target is not None and enforced_target != primary_network
                if enforced is not None
                else bool(environment_network and environment_network != gateway_network)
            )
            if sidecar_gateway and environment_network and target_differs:
                self._docker(
                    [
                        "docker",
                        "network",
                        "connect",
                        enforced_target or environment_network,
                        container_name,
                    ],
                    cwd=task_dir,
                )

            result = _run_monitored(
                ["docker", "start", "-a", container_name],
                cwd=task_dir,
                env=os.environ.copy(),
                run_dir=run_dir,
                task=task,
                trace=self.trace,
                cancel_path=context.cancel_path,
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        self.trace.emit(
            "agent.finished",
            actor="agent",
            data={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "budget_exceeded": result.budget_exceeded,
                "metrics": result.metrics.as_dict(),
            },
        )
        return result

    async def start_session(self, context: AgentRunContext):
        from dataclasses import replace

        from .session import OneShotAgentSession

        context = replace(
            context,
            cancel_path=context.cancel_path or context.run_dir / "agent.control.jsonl",
        )
        return await OneShotAgentSession.start(self, context=context)


def build_agent_adapter(
    spec: AgentSpec,
    *,
    allow_host_agent: bool,
    trace: TraceRecorder,
):
    if spec.type == "cli":
        return CLIAdapter(spec, allow_host=allow_host_agent, trace=trace)
    if spec.type == "docker":
        return DockerAdapter(spec, trace=trace)
    if spec.type == "pi":
        from .pi_container import ContainerPiAdapter

        return ContainerPiAdapter(spec, allow_host=allow_host_agent, trace=trace)
    raise AgentError(f"unsupported agent adapter: {spec.type}")
