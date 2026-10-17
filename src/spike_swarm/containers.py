"""Docker recipe and the long-lived Pi RPC client.

The hardening is a re-implementation of the production container recipe (see
``src/harness/agent.py`` for the reference behaviour): read-only root, every
capability dropped, no privilege escalation, bounded pids/memory/cpu, only the
writable mounts the session needs, and no Docker socket. Model credentials are
passed as bare ``-e NAME`` so their values never appear in the Docker argv.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .models import DEFAULT_TOOLS, SpikeError

CONTAINER_PIDS_LIMIT = 512
CONTAINER_MEMORY = "4g"
CONTAINER_CPUS = "4"
CONTAINER_TMPFS = ("/tmp:rw,nosuid,nodev,size=512m", "/var/tmp:rw,nosuid,nodev,size=256m")
CONTAINER_WORKDIR = "/workspace"
CONTAINER_HOME = "/workspace/home"
CONTAINER_NOTES = "/notes"
CONTAINER_BOARD = "/board"
CONTAINER_INBOX = "/board/inbox"
CONTAINER_GOALS = "/board/goals"
CONTAINER_GRAPH = "/board/intent-fact-graph.md"
CONTAINER_KNOWLEDGE = "/knowledge"
RPC_FRESH_HANDSHAKE_TIMEOUT = 1.0
RPC_RESUME_HANDSHAKE_TIMEOUT = 15.0
# Matches the proven harness reader: one Pi event may embed a large tool
# result, so the stream limit is generous and overruns are drained, not fatal.
RPC_READER_LIMIT_BYTES = 64 * 1024 * 1024
RPC_KILL_GRACE_SECONDS = 5.0

PI_HOST_ENV = (
    ("PI_CODING_AGENT_DIR", "/workspace/.pi-agent"),
    ("PI_CODING_AGENT_SESSION_DIR", "/workspace/.pi-sessions"),
    ("PI_TELEMETRY", "0"),
    ("PI_SKIP_VERSION_CHECK", "1"),
    ("PI_OFFLINE", "1"),
    ("HOME", CONTAINER_HOME),
)


class ContainerError(SpikeError):
    """The container or the Pi RPC session could not be driven any further."""


@dataclass(frozen=True)
class EnvVar:
    """One container environment variable.

    ``value is None`` means "inherit from the runtime's own environment", which
    Docker renders as a bare ``-e NAME`` - the value never reaches the argv.
    """

    name: str
    value: str | None = None


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    read_only: bool = False


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    image: str
    mounts: tuple[Mount, ...]
    environment: tuple[EnvVar, ...]
    network: str = "host"
    # Root inside the container, with every capability dropped: the
    # DAC_OVERRIDE/FOWNER pair is what lets a cap-dropped root write a workspace
    # it does not own (same contract as the Harness's Agent containers), and
    # NET_RAW is what makes a raw-socket scan work at all.
    cap_add: tuple[str, ...] = ("DAC_OVERRIDE", "FOWNER", "NET_RAW")
    workdir: str = CONTAINER_WORKDIR
    entrypoint: tuple[str, ...] = ("sleep", "infinity")
    pids_limit: int = CONTAINER_PIDS_LIMIT
    memory: str = CONTAINER_MEMORY
    cpus: str = CONTAINER_CPUS
    tmpfs: tuple[str, ...] = CONTAINER_TMPFS
    user: str | None = "0:0"


def create_command(spec: ContainerSpec) -> list[str]:
    """Build the single hardened ``docker create`` command for one solver."""

    command = [
        "docker",
        "create",
        "--rm",
        "--pull=never",
        "--name",
        spec.name,
    ]
    if spec.user:
        command.extend(["--user", spec.user])
    command.extend(
        [
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            spec.memory,
            "--cpus",
            spec.cpus,
        ]
    )
    for mount in spec.tmpfs:
        command.extend(["--tmpfs", mount])
    command.extend(["--network", spec.network])
    for capability in spec.cap_add:
        command.extend(["--cap-add", capability])
    for mount in spec.mounts:
        suffix = ":ro" if mount.read_only else ":rw"
        command.extend(["-v", f"{mount.host.resolve()}:{mount.container}{suffix}"])
    command.extend(["-w", spec.workdir])
    for variable in spec.environment:
        if variable.value is None:
            command.extend(["-e", variable.name])
        else:
            command.extend(["-e", f"{variable.name}={variable.value}"])
    command.append(spec.image)
    command.extend(spec.entrypoint)
    return command


def start_command(name: str) -> list[str]:
    return ["docker", "start", name]


def remove_command(name: str) -> list[str]:
    return ["docker", "rm", "-f", name]


def pi_rpc_command(
    *,
    container: str,
    provider: str,
    model: str,
    thinking: str | None,
    tools: Sequence[str] = DEFAULT_TOOLS,
    session_id: str | None = None,
) -> list[str]:
    """Build the long-lived ``docker exec -i ... pi --mode rpc`` command."""

    command = [
        "docker",
        "exec",
        "-i",
        container,
        "pi",
        "--mode",
        "rpc",
    ]
    if session_id:
        command.extend(["--session", session_id])
    command.extend(
        [
            "--no-approve",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-mcp",
            "--offline",
            "--tools",
            ",".join(tools),
            "--provider",
            provider,
            "--model",
            model,
        ]
    )
    if thinking:
        command.extend(["--thinking", thinking])
    return command


class JsonlFramer:
    """Split a byte stream into JSON lines on LF only, keeping the remainder."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        self._buffer += chunk
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()
        return [part.strip() for part in parts if part.strip()]

    def flush(self) -> list[str]:
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


@dataclass
class RpcCounters:
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    malformed_lines: int = 0
    oversized_lines: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "malformed_lines": self.malformed_lines,
            "oversized_lines": self.oversized_lines,
        }


def account_event(event: Mapping[str, object], counters: RpcCounters) -> None:
    """Fold one Pi event into the session counters."""

    event_type = event.get("type")
    if event_type == "tool_execution_start":
        counters.tool_calls += 1
        return
    if event_type != "message_end":
        return
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return
    counters.model_calls += 1
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    counters.input_tokens += _int(usage.get("input"))
    counters.output_tokens += _int(usage.get("output"))
    cost = usage.get("cost")
    if isinstance(cost, dict):
        counters.cost_usd += _float(cost.get("total"))


def _int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def session_event_id(event: Mapping[str, object]) -> str | None:
    if event.get("type") != "session":
        return None
    value = event.get("id")
    return str(value) if value else None


@dataclass
class PiRpcSession:
    """One long-lived Pi RPC process inside one container."""

    container: str
    provider: str
    model: str
    thinking: str | None
    tools: Sequence[str] = DEFAULT_TOOLS
    log_path: Path | None = None
    raw_log_path: Path | None = None
    session_id: str | None = None
    counters: RpcCounters = field(default_factory=RpcCounters)
    settled: bool = True
    events_seen: int = 0
    rounds: int = 0
    last_error: str = ""
    _command_failed: bool = field(default=False, repr=False)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _framer: JsonlFramer = field(default_factory=JsonlFramer, repr=False)
    _log_handle: object = field(default=None, repr=False)
    _stderr_handle: object = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def command(self) -> list[str]:
        return pi_rpc_command(
            container=self.container,
            provider=self.provider,
            model=self.model,
            thinking=self.thinking,
            tools=self.tools,
            session_id=self.session_id,
        )

    async def start(
        self,
        *,
        ready_timeout: float = RPC_RESUME_HANDSHAKE_TIMEOUT,
        require_session: bool = False,
    ) -> None:
        """Spawn the RPC process and capture its session identity.

        Pi may stay silent until the first command, so the session record is a
        best-effort capture on a fresh start and a required handshake on resume.
        Whatever the process writes first is what the runtime waits for.
        """

        if shutil.which("docker") is None:
            raise ContainerError("docker executable was not found")
        if self.raw_log_path is not None:
            self.raw_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.raw_log_path.open("a", encoding="utf-8")
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self.log_path.open("a", encoding="utf-8")
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_handle,
            start_new_session=True,
            limit=RPC_READER_LIMIT_BYTES,
        )
        timeout = ready_timeout if require_session else min(RPC_FRESH_HANDSHAKE_TIMEOUT, ready_timeout)
        await self._capture_session_identity(timeout=timeout, required=require_session)
        if not self.alive:
            raise ContainerError(
                f"pi RPC session exited during startup: {self.container}"
                + (f" (see {self.log_path})" if self.log_path else "")
            )
        await self.send(
            {
                "id": f"spike-steering-mode-{os.getpid()}",
                "type": "set_steering_mode",
                "mode": "all",
            }
        )

    async def _capture_session_identity(self, *, timeout: float, required: bool) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if required:
                    raise ContainerError(
                        f"pi RPC did not report a session id: {self.container}"
                    )
                return
            event = await self.read_event(timeout=remaining)
            if event is None:
                if not self.alive:
                    if required:
                        raise ContainerError(
                            f"pi RPC exited before reporting its session: {self.container}"
                        )
                    return
                continue
            identifier = session_event_id(event)
            if identifier:
                self.session_id = identifier
                return

    async def send(self, command: Mapping[str, object]) -> None:
        if self._process is None or self._process.stdin is None:
            raise ContainerError("pi RPC session is not running")
        payload = json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        await self._process.stdin.drain()

    async def prompt(self, message: str) -> None:
        self.last_error = ""
        self.rounds += 1
        await self.send({"id": f"spike-round-{self.rounds}", "type": "prompt", "message": message})
        self.settled = False

    async def steer(self, message: str) -> None:
        self.last_error = ""
        self.rounds += 1
        await self.send({"id": f"spike-round-{self.rounds}", "type": "steer", "message": message})

    async def send_round(self, message: str) -> None:
        """Prompt a settled session, steer one that is still working."""

        if self.settled:
            await self.prompt(message)
        else:
            await self.steer(message)

    async def read_event(self, *, timeout: float) -> Mapping[str, object] | None:
        """Read the next JSON event; ``None`` when the process ended."""

        assert self._process is not None and self._process.stdout is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                return None
            except (asyncio.LimitOverrunError, ValueError):
                # One event larger than the reader limit. Drain what is buffered
                # and let the framer split it, so a single huge tool result
                # costs one oversized line instead of the whole stream.
                self.counters.oversized_lines += 1
                try:
                    chunk = await asyncio.wait_for(
                        self._process.stdout.read(RPC_READER_LIMIT_BYTES),
                        timeout=max(0.001, remaining),
                    )
                except TimeoutError:
                    return None
                if not chunk:
                    return None
            except asyncio.IncompleteReadError as exc:
                chunk = exc.partial
                if not chunk:
                    return None
            text = chunk.decode("utf-8", errors="replace")
            if self._log_handle is not None:
                self._log_handle.write(text)
                self._log_handle.flush()
            for line in self._framer.feed(text):
                event = self._parse(line)
                if event is not None:
                    return event
            if not text.endswith("\n") and self._process.returncode is not None:
                for line in self._framer.flush():
                    event = self._parse(line)
                    if event is not None:
                        return event
                return None

    def _parse(self, line: str) -> Mapping[str, object] | None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.counters.malformed_lines += 1
            return None
        if not isinstance(event, dict):
            self.counters.malformed_lines += 1
            return None
        self.events_seen += 1
        account_event(event, self.counters)
        identifier = session_event_id(event)
        if identifier:
            self.session_id = identifier
        if event.get("type") == "response" and event.get("success") is False:
            # Pi rejects a prompt outright (missing credential, bad model) and
            # never settles that turn, so the round has to end on the error.
            self.last_error = str(event.get("error") or "command failed").strip()
            self._command_failed = True
        elif event.get("type") == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("stopReason") == "error":
                detail = str(message.get("errorMessage") or "model error").strip()
                self.last_error = f"{message.get('provider', 'provider')}: {detail}"
        return event

    async def wait_settled(self, *, timeout: float) -> bool:
        """Read until ``agent_settled``; True when the turn completed in time."""

        if self._command_failed:
            self._command_failed = False
            self.settled = True
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            event = await self.read_event(timeout=remaining)
            if event is None:
                return False
            if self._command_failed:
                self._command_failed = False
                self.settled = True
                return False
            if event.get("type") == "agent_settled":
                self.settled = True
                return True

    async def stop(self) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, AttributeError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=RPC_KILL_GRACE_SECONDS)
            except TimeoutError:  # pragma: no cover - defensive
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    pass
        for handle in (self._log_handle, self._stderr_handle):
            if handle is not None:
                try:
                    handle.close()  # type: ignore[attr-defined]
                except OSError:  # pragma: no cover - defensive
                    pass
        self._log_handle = None
        self._stderr_handle = None


async def run_docker(command: Sequence[str], *, timeout: float = 60.0) -> tuple[int, str]:
    """Run one docker subcommand, returning ``(returncode, output)``."""

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:  # pragma: no cover - defensive
        process.kill()
        return 124, "timeout"
    return process.returncode or 0, stdout.decode("utf-8", errors="replace").strip()


async def ensure_container(spec: ContainerSpec) -> None:
    """Create and start one container, failing closed with Docker's message."""

    for directory in _writable_dirs(spec):
        directory.mkdir(parents=True, exist_ok=True)
    code, output = await run_docker(create_command(spec))
    if code != 0:
        raise ContainerError(f"docker create failed for {spec.name}: {output}")
    code, output = await run_docker(start_command(spec.name))
    if code != 0:
        await run_docker(remove_command(spec.name))
        raise ContainerError(f"docker start failed for {spec.name}: {output}")


async def destroy_container(name: str) -> None:
    if shutil.which("docker") is None:  # pragma: no cover - defensive
        return
    await run_docker(remove_command(name), timeout=60.0)


def _writable_dirs(spec: ContainerSpec) -> list[Path]:
    return [mount.host for mount in spec.mounts if not mount.read_only]


def solver_env(
    *,
    session_id: str,
    challenge: str,
    objective: str,
    inbox_file: str,
    goals_file: str,
    round_seconds: int,
    addresses: Sequence[str],
) -> tuple[EnvVar, ...]:
    """The spike's own container contract, plus Pi's runtime environment."""

    variables = (
        EnvVar("SPIKE_SESSION_ID", session_id),
        EnvVar("SPIKE_CHALLENGE", challenge),
        EnvVar("SPIKE_OBJECTIVE", objective),
        EnvVar("SPIKE_TARGETS", " ".join(addresses)),
        EnvVar("SPIKE_BOARD_INBOX", inbox_file),
        EnvVar("SPIKE_BOARD_GOALS", goals_file),
        EnvVar("SPIKE_GRAPH_FILE", CONTAINER_GRAPH),
        EnvVar("SPIKE_NOTES_DIR", CONTAINER_NOTES),
        EnvVar("SPIKE_ROUND_SECONDS", str(round_seconds)),
    )
    return variables + tuple(EnvVar(name, value) for name, value in PI_HOST_ENV)


def passthrough_env(names: Sequence[str], *, environ: Mapping[str, str] | None = None) -> tuple[EnvVar, ...]:
    """Credential pass-through as bare ``-e NAME`` entries, values never in argv."""

    env = os.environ if environ is None else environ
    variables: list[EnvVar] = []
    for name in names:
        cleaned = name.strip()
        if not cleaned:
            continue
        if cleaned not in env:
            raise ContainerError(f"requested pass-through variable is missing: {cleaned}")
        if "TOKEN" in cleaned.upper():
            raise ContainerError(
                f"refusing to pass a token-like variable into an agent container: {cleaned}"
            )
        variables.append(EnvVar(cleaned))
    return tuple(variables)


async def container_running(name: str) -> bool:
    """True when Docker reports the named container running."""

    if shutil.which("docker") is None:
        return False
    code, output = await run_docker(
        ["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=30.0
    )
    return code == 0 and output.strip() == "true"
