from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .agent import (
    AGENT_EVENT_FILE,
    NORMALIZED_EVENT_FILE,
    AgentError,
    AgentResult,
    AgentRunContext,
    ContainerLimits,
    _terminate_process,
    build_container_command,
    run_docker,
)
from .agent_workspace import prepare_agent_task_view
from .budget import FINALIZE_GRACE_SECONDS, BudgetMonitor, UsageMetrics, charge_usage_event
from .models import AgentSpec, TaskSpec
from .pi_adapter import PiAdapter
from .secureio import open_regular_file, read_regular_text
from .session import (
    RPC_AGENT_CAPABILITIES,
    AgentCapabilities,
    AgentCheckpoint,
    AgentEvent,
    AgentObservation,
    OneShotAgentSession,
    SessionPhase,
    SessionPhaseTracker,
    append_control,
    append_feedback,
    control_record,
    feedback_record,
)
from .trace import TraceRecorder

# How long to wait for the ``session`` record Pi emits when resuming.
RPC_RESUME_HANDSHAKE_TIMEOUT = 15.0
# Fresh RPC runs get a short best-effort read: Pi 1.0.4 does not announce a
# session record until it is asked to resume one, and blocking startup on a
# record that never arrives would deadlock before the objective is sent.
RPC_FRESH_HANDSHAKE_TIMEOUT = 1.0
# Bounded ingestion of the Agent's own typed event file.
MAX_AGENT_EVENT_CHUNK_BYTES = 1024 * 1024
MAX_AGENT_EVENT_LINE_BYTES = 256 * 1024


def _container_name(run_dir: Path) -> str:
    """Stable per-run container name.

    Deriving the name from the run directory's own name collides for benchmark
    runs (whose workspace directory is always ``agent-workspace``), which breaks
    parallel execution. Hash the resolved path instead so the name is unique per
    run and stable across resume.
    """
    digest = hashlib.sha256(str(run_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    return f"harness_pi_{digest}"


class ContainerPiSession(OneShotAgentSession):
    """Session wrapper that can actively terminate the running Pi container."""

    def __init__(self, adapter, *, context: AgentRunContext, poll_interval=0.05) -> None:
        super().__init__(adapter, context=context, poll_interval=poll_interval)
        self.container_name = _container_name(self.run_dir)

    async def close(self, reason: str) -> None:
        await super().close(reason)
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "kill", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


class ContainerPiRpcSession:
    """Interactive Pi RPC session with Harness-controlled stdin steering."""

    _STEER_TYPES = frozenset(
        {
            "solver.verification",
            "solver.plan.updated",
            "solver.replan_requested",
            "aci.feedback",
            "benchmark.feedback",
            "world.state.updated",
            "solver.budget",
            "solver.finalize",
            "solver.round",
            "solver.hint",
        }
    )

    def __init__(self, adapter: ContainerPiAdapter, *, context: AgentRunContext) -> None:
        self.adapter = adapter
        self.context = context
        self.run_dir = Path(context.run_dir)
        self.container_name = _container_name(self.run_dir)
        self.event_path = self.run_dir / "events.jsonl"
        # Agent-authored typed solver events. RPC sessions read Pi's stdout, so
        # these reach the solver only through this cursor (see
        # ``_consume_agent_events``).
        self.agent_event_path = self.run_dir / AGENT_EVENT_FILE
        self.feedback_path = self.run_dir / "agent.feedback.jsonl"
        self.control_path = self.run_dir / "agent.control.jsonl"
        self.raw_path = self.run_dir / "agent.stdout.log"
        self.stderr_path = self.run_dir / "agent.stderr.log"
        self.gateway_usage_path = (
            Path(context.gateway_usage_path) if context.gateway_usage_path else None
        )
        self._gateway_event_offset = int(context.resume_gateway_event_offset or 0)
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_handle = None
        self._write_lock = asyncio.Lock()
        self._pending_observations: list[AgentObservation] = []
        self._prompt_sent = False
        self._events_consumed = False
        self._closed = False
        self._settled = False
        self._timed_out = False
        self._budget_exceeded: str | None = None
        self._event_offset = 0
        self._agent_event_offset = 0
        self._agent_event_remainder = ""
        self._agent_event_line = 0
        self._feedback_offset = 0
        self._rpc_line = 0
        self._rpc_epoch = uuid.uuid4().hex
        self._startup_records: list[bytes] = []
        self._pi_session_id: str | None = None
        self._started = 0.0
        self._metrics = UsageMetrics()
        self._task = context.task
        self._gateway_enabled = bool(context.gateway_url and context.gateway_token)
        self.phase = SessionPhaseTracker(trace=adapter.trace)

    @property
    def capabilities(self) -> AgentCapabilities:
        return RPC_AGENT_CAPABILITIES

    @classmethod
    async def start(
        cls,
        adapter: ContainerPiAdapter,
        *,
        context: AgentRunContext,
    ) -> ContainerPiRpcSession:
        session = cls(adapter, context=context)
        try:
            await session._start()
        except Exception:
            if session._stderr_handle is not None:
                session._stderr_handle.close()
                session._stderr_handle = None
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rm", "-f", session.container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            raise
        return session

    async def _start(self) -> None:
        from .network_policy import enforced_network

        context = self.context
        task = context.task
        task_dir = Path(context.task_dir)
        agent_task_dir = context.agent_task_dir
        if agent_task_dir is None:
            agent_task_dir = prepare_agent_task_view(
                task_dir=task_dir,
                run_root=self.run_dir.parent,
                verifier_entrypoint=task.verification.entrypoint,
            )
        run_dir = self.run_dir
        if context.resume_session_id:
            await self._guard_existing_resume_container(task_dir)
        environment_network = context.environment_network
        gateway_url = context.gateway_url
        gateway_token = context.gateway_token
        gateway_network = context.gateway_network
        gateway_enabled = bool(gateway_url and gateway_token)
        enforced = enforced_network(
            self.adapter.spec,
            environment_network=environment_network,
            gateway_network=gateway_network,
            gateway_enabled=gateway_enabled,
        )
        if gateway_enabled and (
            self.adapter.spec.network == "none" or self.adapter.spec.network_profile == "offline"
        ):
            raise AgentError("Pi offline networking is incompatible with per-run Gateway")

        sidecar_gateway = gateway_enabled and gateway_network is not None
        if enforced is not None:
            primary_network = enforced.primary
        elif self.adapter.spec.network_profile == "offline":
            primary_network = "none"
        elif sidecar_gateway:
            if self.adapter.spec.network == "host":
                raise AgentError("Pi network:host is incompatible with sidecar Gateway mode")
            primary_network = str(gateway_network)
        elif self.adapter.spec.network == "host":
            primary_network = "host"
            if gateway_enabled:
                gateway_url = gateway_url.replace("host.docker.internal", "127.0.0.1")
        elif self.adapter.spec.network == "environment":
            primary_network = environment_network or "bridge"
        else:
            primary_network = "none"

        self.event_path.write_text("", encoding="utf-8")
        self.raw_path.write_text("", encoding="utf-8")
        self.stderr_path.write_text("", encoding="utf-8")
        self.feedback_path.touch(exist_ok=True)
        self.control_path.touch(exist_ok=True)
        (run_dir / "pi-sessions").mkdir(parents=True, exist_ok=True)
        (run_dir / "home").mkdir(parents=True, exist_ok=True)
        self.adapter._prepare_agent_dir(
            run_dir=run_dir,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
        )
        host_gateway = gateway_enabled and not sidecar_gateway and primary_network != "host"
        create_command = self.adapter._container_command(
            task,
            task_dir=task_dir,
            agent_task_dir=agent_task_dir,
            run_dir=run_dir,
            container_name=self.container_name,
            network=primary_network,
            environment_project=context.environment_project,
            seed=context.seed,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            host_gateway=host_gateway,
            agent_session_id=context.resume_session_id,
        )
        self.adapter.trace.emit(
            "agent.started",
            actor="agent",
            data={
                "agent_id": self.adapter.spec.id,
                "type": "pi",
                "execution": "docker",
                "image": self.adapter.spec.image,
                "network": primary_network,
                "target_network": environment_network,
                "runtime": self.adapter.spec.runtime,
                "model": self.adapter.pi.model,
                "provider": "harness" if gateway_enabled else self.adapter.pi.provider,
                "thinking": self.adapter.pi.thinking,
                "tools": self.adapter.pi.tools,
                "pi_protocol": "rpc",
            },
        )
        await asyncio.to_thread(self.adapter._docker, create_command, cwd=task_dir)
        enforced_target = enforced.environment if enforced is not None else None
        target_differs = (
            enforced_target is not None and enforced_target != primary_network
            if enforced is not None
            else bool(environment_network and environment_network != gateway_network)
        )
        if sidecar_gateway and environment_network and target_differs:
            await asyncio.to_thread(
                self.adapter._docker,
                [
                    "docker",
                    "network",
                    "connect",
                    str(enforced_target or environment_network),
                    self.container_name,
                ],
                cwd=task_dir,
            )
        self._stderr_handle = open_regular_file(self.stderr_path, "ab")
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "-ai",
            self.container_name,
            cwd=task_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_handle,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        self._started = time.monotonic()
        if context.resume_session_id:
            await self._wait_for_session_identity(
                timeout=RPC_RESUME_HANDSHAKE_TIMEOUT, required=True
            )
        else:
            # Best effort only: capture the record if Pi emits one, otherwise
            # proceed and let ``events()`` send the objective as the first
            # command and pick up any later session record.
            await self._wait_for_session_identity(
                timeout=RPC_FRESH_HANDSHAKE_TIMEOUT, required=False
            )
        self.phase.transition(
            SessionPhase.STARTED,
            resumed=bool(context.resume_session_id),
            session_id=self._pi_session_id,
        )

    async def _wait_for_session_identity(self, *, timeout: float, required: bool) -> None:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Pi RPC process has no stdout")
        try:
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout)
                if not line:
                    if required:
                        raise AgentError("Pi RPC exited before reporting its session ID")
                    return
                self._startup_records.append(line)
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "session":
                    continue
                session_id = record.get("id")
                if not isinstance(session_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9._-]{1,128}", session_id
                ):
                    raise AgentError("Pi RPC reported an invalid session ID")
                requested_id = self.context.resume_session_id
                if requested_id is not None and requested_id != session_id:
                    raise AgentError("Pi RPC did not restore the requested session ID")
                self._pi_session_id = session_id
                return
        except TimeoutError as exc:
            if required:
                raise AgentError("Pi RPC did not report a session ID during startup") from exc

    async def _guard_existing_resume_container(self, task_dir: Path) -> None:
        inspect = await asyncio.to_thread(
            self.adapter._docker,
            ["docker", "inspect", "--format", "{{.State.Running}}", self.container_name],
            cwd=task_dir,
            check=False,
        )
        if inspect.returncode != 0:
            if "no such object" not in inspect.stderr.lower() and "no such container" not in inspect.stderr.lower():
                raise AgentError(f"cannot inspect prior Pi container: {inspect.stderr.strip()}")
            return
        if inspect.stdout.strip().lower() == "true":
            raise AgentError(
                "prior Pi container is still running; stop it and reconcile any in-flight "
                "action before resuming"
            )
        await asyncio.to_thread(
            self.adapter._docker,
            ["docker", "rm", "-f", self.container_name],
            cwd=task_dir,
            check=False,
        )

    def _normalize_pi_record(self, raw: str) -> list[tuple[str, dict[str, Any]]]:
        """Keep model accounting on the trusted Gateway stream when routed there."""
        return self.adapter._normalize_line(
            raw,
            account_model=not self._gateway_enabled,
        )

    async def events(self) -> AsyncIterator[AgentEvent]:
        if self.process is None:
            raise RuntimeError("session has not been started")
        if self._events_consumed:
            raise RuntimeError("Pi RPC event stream can only be consumed once")
        self._events_consumed = True
        task = self._task
        initial_prompt = self.adapter._prompt_text(task)
        if self.context.resume_session_id:
            initial_prompt = (
                "Continue the existing Pi session for this Harness run. First read the current "
                "world.context.txt and progress.json in the project workspace. Treat them as "
                "the current Harness state, avoid repeating completed or interrupted actions, "
                "and continue the objective using only new safe actions.\n\n" + initial_prompt
            )
        if self._pending_observations:
            initial_prompt += "\n\nHarness state before the first model turn:\n" + "\n".join(
                self._format_observation(item) for item in self._pending_observations
            )
            self._pending_observations.clear()
        self._prompt_sent = True
        self.phase.transition(SessionPhase.PROMPTING)
        await self._send_command(
            {
                "id": f"harness-steering-mode-{uuid.uuid4().hex}",
                "type": "set_steering_mode",
                "mode": "all",
            }
        )
        await self._send_command(
            {
                "id": f"harness-prompt-{uuid.uuid4().hex}",
                "type": "prompt",
                "message": initial_prompt,
            }
        )
        assert self.process.stdout is not None
        startup_records = list(self._startup_records)
        self._startup_records.clear()
        while self.process.returncode is None:
            for event in self._consume_agent_events():
                yield event
            hard_deadline = task.budgets.wall_time + FINALIZE_GRACE_SECONDS
            remaining = hard_deadline - (time.monotonic() - self._started)
            if remaining <= 0:
                self._timed_out = True
                self.adapter.trace.emit(
                    "budget.exceeded",
                    data={
                        "budget": "wall_time",
                        "limit": task.budgets.wall_time,
                        "grace_seconds": FINALIZE_GRACE_SECONDS,
                    },
                )
                await self._kill_container()
                break
            try:
                line = startup_records.pop(0) if startup_records else await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=min(remaining, 0.1)
                )
            except TimeoutError:
                # The loop top re-checks the deadline every pass, so a wait
                # that overshot it is caught there on the next iteration.
                self._consume_gateway_events()
                if self._budget_exceeded:
                    await self._kill_container()
                    break
                continue
            self._consume_gateway_events()
            if self._budget_exceeded:
                await self._kill_container()
                break
            if not line:
                break
            self._rpc_line += 1
            raw = line.decode("utf-8", errors="replace")
            with open_regular_file(self.raw_path, "a") as output:
                output.write(raw)
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                record = None
            if isinstance(record, dict) and record.get("type") == "response":
                if record.get("success") is False:
                    command_id = record.get("id")
                    event = AgentEvent(
                        type="agent.telemetry_error",
                        data={
                            "source": "pi.rpc",
                            "command_id_sha256": (
                                hashlib.sha256(command_id.encode("utf-8")).hexdigest()
                                if isinstance(command_id, str)
                                else None
                            ),
                            "error_type": type(record.get("error")).__name__,
                            "message": "Pi RPC command failed",
                        },
                        event_id=f"pi-rpc:{self._rpc_epoch}:{self._rpc_line}:0",
                    )
                    self._persist_event(event)
                    yield event
                continue
            if isinstance(record, dict) and record.get("type") == "session":
                session_id = record.get("id")
                if isinstance(session_id, str) and 0 < len(session_id) <= 256:
                    self._pi_session_id = session_id
            normalized = self._normalize_pi_record(raw.rstrip("\r\n"))
            for index, (event_type, data) in enumerate(normalized):
                event = AgentEvent(
                    type=event_type,
                    data=data,
                    event_id=f"pi-rpc:{self._rpc_epoch}:{self._rpc_line}:{index}",
                )
                self._persist_event(event)
                self._account(event)
                self.adapter.trace.emit(event.type, actor="agent", data=event.data)
                if event.type == "pi.agent_settled":
                    self._settled = True
                    self.phase.transition(SessionPhase.SETTLED)
                yield event
            if self._budget_exceeded:
                await self._kill_container()
                break
            if self._settled:
                break

        for event in self._consume_agent_events():
            yield event
        if not self._settled and not self._timed_out and not self._budget_exceeded:
            self.phase.transition(
                SessionPhase.CRASHED, returncode=self.process.returncode
            )
            event = AgentEvent(
                type="agent.session.crashed",
                data={"returncode": self.process.returncode},
                event_id=f"pi-rpc:{self._rpc_epoch}:{self._rpc_line}:crash",
            )
            self._persist_event(event)
            yield event

    async def observe(self, observation: AgentObservation) -> None:
        self._feedback_offset = await asyncio.to_thread(
            append_feedback, self.feedback_path, feedback_record(observation)
        )
        if not self._prompt_sent:
            self._pending_observations.append(observation)
        elif observation.type in self._STEER_TYPES and not self._closed:
            try:
                settled = self._settled
                await self._send_command(
                    {
                        "id": f"harness-steer-{uuid.uuid4().hex}",
                        "type": "prompt" if settled else "steer",
                        "message": self._format_observation(observation),
                    }
                )
                if settled:
                    # A settled RPC agent is idle between turns. Prompt starts
                    # the next turn; steer is reserved for an active turn.
                    self._settled = False
                    self.phase.transition(SessionPhase.PROMPTING, cause="steer_prompt")
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                self.adapter.trace.emit(
                    "agent.rpc.steer_failed",
                    actor="harness",
                    data={"observation_type": observation.type, "error_type": type(exc).__name__},
                )

    async def checkpoint(self) -> AgentCheckpoint:
        return AgentCheckpoint(
            id=f"pi-rpc-{self.container_name}-{self._rpc_line}",
            event_offset=self._event_offset,
            feedback_offset=self._feedback_offset,
            session_id=self._pi_session_id,
            gateway_event_offset=self._gateway_event_offset,
        )

    @property
    def usage_metrics(self) -> dict[str, int | float]:
        return self._metrics.as_dict()

    async def close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.phase.transition(SessionPhase.CLOSED, reason=reason)
        await asyncio.to_thread(append_control, self.control_path, control_record(reason))
        await self._kill_container()

    async def result(self) -> AgentResult:
        if self.process is None:
            raise RuntimeError("session has not been started")
        if self.process.returncode is None and not self._closed:
            await self._kill_container()
            self._closed = True
            self.phase.transition(SessionPhase.CLOSED, reason="result_drain")
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        drain_task = asyncio.create_task(self._drain_stdout())
        try:
            returncode = await asyncio.wait_for(self.process.wait(), timeout=15)
        except TimeoutError:
            await self._kill_container()
            try:
                returncode = await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                returncode = -1
        await drain_task
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "rm", "-f", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        result = AgentResult(
            returncode=returncode,
            timed_out=self._timed_out,
            budget_exceeded=self._budget_exceeded,
            stdout=read_regular_text(self.raw_path),
            stderr=read_regular_text(self.stderr_path),
            metrics=self._metrics,
        )
        self.adapter.trace.emit(
            "agent.finished",
            actor="agent",
            data={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "budget_exceeded": result.budget_exceeded,
                "metrics": result.metrics.as_dict(),
                "pi_protocol": "rpc",
                "containerized": True,
            },
        )
        return result

    def _persist_event(self, event: AgentEvent) -> None:
        payload = {"type": event.type, "data": event.data, "event_id": event.event_id}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open_regular_file(self.event_path, "a") as handle:
            handle.write(encoded)
            handle.flush()
            self._event_offset = handle.tell()

    def _consume_agent_events(self) -> list[AgentEvent]:
        """Read typed solver events the Agent appended to its own event file.

        The RPC session consumes Pi's stdout, so anything the Agent writes to
        ``$HARNESS_EVENT_FILE`` (``action.intent``, ``world.observe``, ...) only
        reaches the solver through this cursor. The file is untrusted: partial
        trailing lines are buffered, oversized lines and malformed JSON become
        telemetry errors instead of crashing the stream, and a truncated or
        replaced file restarts the cursor.
        """
        events: list[AgentEvent] = []
        try:
            size = self.agent_event_path.stat().st_size
        except OSError:
            return events
        if size < self._agent_event_offset:
            self._agent_event_offset = 0
            self._agent_event_remainder = ""
            self.adapter.trace.emit(
                "agent.events.reset",
                actor="harness",
                data={"reason": "file_truncated_or_replaced"},
            )
        try:
            with open_regular_file(self.agent_event_path, "r", errors="replace") as handle:
                handle.seek(self._agent_event_offset)
                chunk = handle.read(MAX_AGENT_EVENT_CHUNK_BYTES)
                self._agent_event_offset = handle.tell()
        except OSError:
            return events
        if not chunk:
            return events

        lines = (self._agent_event_remainder + chunk).splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._agent_event_remainder = lines.pop()
        else:
            self._agent_event_remainder = ""
        if len(self._agent_event_remainder) > MAX_AGENT_EVENT_LINE_BYTES:
            self._agent_event_remainder = ""
            events.append(
                self._agent_event_error("line exceeds the event size limit", line="")
            )

        for raw in lines:
            stripped = raw.strip()
            if not stripped:
                continue
            self._agent_event_line += 1
            if len(raw) > MAX_AGENT_EVENT_LINE_BYTES:
                events.append(
                    self._agent_event_error("line exceeds the event size limit", line="")
                )
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                events.append(
                    self._agent_event_error("invalid JSON event", line=stripped[:200])
                )
                continue
            if not isinstance(payload, dict):
                events.append(
                    self._agent_event_error("event must be a JSON object", line=stripped[:200])
                )
                continue
            data = payload.get("data")
            event = AgentEvent(
                type=str(payload.get("type", "unknown")),
                data=data if isinstance(data, dict) else {},
                event_id=f"agent-event-line:{self._agent_event_line}",
            )
            self._persist_event(event)
            events.append(event)
        return events

    def _agent_event_error(self, message: str, *, line: str) -> AgentEvent:
        event = AgentEvent(
            type="agent.telemetry_error",
            data={"source": "agent.event_file", "message": message, "line": line},
            event_id=f"agent-event-line:{self._agent_event_line}",
        )
        self._persist_event(event)
        return event

    async def _send_command(self, command: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.stdin.is_closing():
            return
        line = json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self.process.stdin.write(line.encode("utf-8"))
            await self.process.stdin.drain()

    @staticmethod
    def _format_observation(observation: AgentObservation) -> str:
        payload = json.dumps(observation.data, ensure_ascii=False, sort_keys=True)
        return f"Harness feedback ({observation.type}): {payload}"

    def _account(self, event: AgentEvent) -> None:
        if charge_usage_event(self._metrics, event.type, event.data):
            return
        missing = event.type == "model.usage_missing"
        self._budget_exceeded = "invalid_telemetry"
        self.adapter.trace.emit(
            "agent.telemetry_error",
            data={
                "message": (
                    "model usage telemetry was missing"
                    if missing
                    else "invalid or negative Pi usage telemetry"
                )
            },
        )
        self.adapter.trace.emit(
            "budget.exceeded",
            data={
                "budget": self._budget_exceeded,
                "reason": (
                    "model usage telemetry was missing"
                    if missing
                    else "untrusted Pi usage telemetry was invalid"
                ),
            },
        )

    def _consume_gateway_events(self) -> None:
        if self.gateway_usage_path is None:
            return
        try:
            with open_regular_file(self.gateway_usage_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if self._gateway_event_offset > handle.tell():
                    self._invalid_gateway_telemetry("gateway usage cursor is ahead of its log")
                    return
                handle.seek(self._gateway_event_offset)
                while True:
                    line = handle.readline(8 * 1024 * 1024 + 1)
                    if not line:
                        return
                    if len(line) > 8 * 1024 * 1024:
                        self._invalid_gateway_telemetry("gateway usage record exceeded size limit")
                        return
                    if not line.endswith(b"\n"):
                        return
                    self._gateway_event_offset = handle.tell()
                    self._consume_gateway_event_line(line)
                    if self._budget_exceeded:
                        return
        except FileNotFoundError:
            if self._gateway_event_offset:
                self._invalid_gateway_telemetry("gateway usage log is missing at the saved cursor")
            return

    def _consume_gateway_event_line(self, line: bytes) -> None:
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._invalid_gateway_telemetry("gateway usage log contains malformed JSON")
            return
        if isinstance(record, dict) and record.get("type") == "model.usage_missing":
            self.adapter.trace.emit("model.usage_missing", actor="gateway")
            self._invalid_gateway_telemetry("gateway did not report valid model usage")
            return
        if not isinstance(record, dict) or record.get("type") not in {
            "model.request",
            "model.usage",
        }:
            return
        data = record.get("data")
        if not isinstance(data, dict):
            self._invalid_gateway_telemetry("gateway usage record has invalid shape")
            return
        event = AgentEvent(
            type=record["type"],
            data=data,
            event_id=f"gateway:{self._gateway_event_offset}",
        )
        self._account(event)
        self.adapter.trace.emit(event.type, actor="gateway", data=event.data)

    def _invalid_gateway_telemetry(self, reason: str) -> None:
        self._budget_exceeded = "invalid_telemetry"
        self.adapter.trace.emit("agent.telemetry_error", data={"message": reason})
        self.adapter.trace.emit(
            "budget.exceeded",
            data={"budget": self._budget_exceeded, "reason": reason},
        )

    async def _kill_container(self) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "kill", self.container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    async def _drain_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        while line := await self.process.stdout.readline():
            with open_regular_file(self.raw_path, "ab") as output:
                output.write(line)


class ContainerPiAdapter(PiAdapter):
    """Run Pi inside a restricted Docker container while reusing Pi JSON parsing."""

    def __init__(self, spec: AgentSpec, *, allow_host: bool, trace: TraceRecorder) -> None:
        del allow_host
        if shutil.which("docker") is None:
            raise AgentError("docker executable was not found")
        if spec.pi is None:
            raise AgentError("Pi agent requires a pi configuration block")
        if not spec.image:
            raise AgentError("Pi agent requires a container image")
        self.spec = spec
        self.pi = spec.pi
        self.trace = trace
        self._tool_started: dict[str, float] = {}

    _docker = staticmethod(run_docker)

    def _container_command(
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
        agent_session_id: str | None = None,
        agent_task_dir: Path | None = None,
    ) -> list[str]:
        pi_env = {
            "PI_CODING_AGENT_DIR": "/run/harness/pi-agent",
            "PI_CODING_AGENT_SESSION_DIR": "/run/harness/pi-sessions",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
            "HOME": "/run/harness/home",
        }
        if self.pi.offline or self.spec.network_profile != "unrestricted":
            pi_env["PI_OFFLINE"] = "1"
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
            workdir="/run/harness",
            limits=ContainerLimits(
                pids=512,
                memory="4g",
                cpus="4",
                tmpfs=(
                    "/tmp:rw,nosuid,nodev,size=512m",
                    "/var/tmp:rw,nosuid,nodev,size=256m",
                ),
            ),
            reserved_env_names=frozenset(self.spec.env) | frozenset(self.pi.env_passthrough),
            extra_cap_add=tuple(self.pi.cap_add),
            extra_env=pi_env,
            env_passthrough=tuple(self.pi.env_passthrough),
            interactive=self.pi.mode == "rpc",
            event_file_name=(
                AGENT_EVENT_FILE if self.pi.mode == "rpc" else NORMALIZED_EVENT_FILE
            ),
            image_command=[
                str(self.spec.image),
                *self._command(
                    task,
                    gateway_enabled=bool(gateway_url and gateway_token),
                    session_id=agent_session_id,
                ),
            ],
        )

    @property
    def capabilities(self) -> AgentCapabilities:
        from .session import ONE_SHOT_AGENT_CAPABILITIES

        if self.pi.mode == "rpc":
            return RPC_AGENT_CAPABILITIES
        return ONE_SHOT_AGENT_CAPABILITIES

    async def start_session(
        self,
        context: AgentRunContext,
    ) -> ContainerPiSession | ContainerPiRpcSession:
        if self.pi.mode == "rpc":
            return await ContainerPiRpcSession.start(self, context=context)
        if context.resume_session_id is not None:
            raise AgentError("Pi JSON mode cannot restore an Agent session")
        return await ContainerPiSession.start(self, context=context)

    def run(self, context: AgentRunContext) -> AgentResult:
        if self.pi.mode == "rpc":
            raise AgentError(
                "Pi RPC mode requires start_session(); use mode: json for one-shot runs"
            )
        if context.resume_session_id is not None:
            raise AgentError("Pi JSON mode cannot restore an Agent session")
        from .network_policy import enforced_network

        task = context.task
        task_dir = context.task_dir
        run_dir = context.run_dir
        environment_network = context.environment_network
        gateway_url = context.gateway_url
        gateway_token = context.gateway_token
        gateway_network = context.gateway_network
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
            raise AgentError("Pi offline networking is incompatible with per-run Gateway")

        if self.spec.network == "host" and gateway_url and gateway_network is None:
            gateway_url = gateway_url.replace("host.docker.internal", "127.0.0.1")

        event_file = run_dir / "events.jsonl"
        event_file.write_text("", encoding="utf-8")
        raw_path = run_dir / "agent.stdout.log"
        stderr_path = run_dir / "agent.stderr.log"
        raw_path.write_text("", encoding="utf-8")
        self._prepare_agent_dir(
            run_dir=run_dir,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
        )
        (run_dir / "pi-sessions").mkdir(parents=True, exist_ok=True)
        (run_dir / "home").mkdir(parents=True, exist_ok=True)

        container_name = _container_name(run_dir)
        sidecar_gateway = gateway_enabled and gateway_network is not None
        host_gateway = gateway_enabled and not sidecar_gateway and self.spec.network != "host"
        if enforced is not None:
            primary_network = enforced.primary
        elif self.spec.network_profile == "offline":
            primary_network = "none"
        elif sidecar_gateway:
            if self.spec.network == "host":
                raise AgentError("Pi network:host is incompatible with sidecar Gateway mode")
            primary_network = str(gateway_network)
        elif self.spec.network == "host":
            primary_network = "host"
        elif self.spec.network == "environment":
            primary_network = environment_network or "bridge"
        else:
            primary_network = "none"

        create_command = self._container_command(
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
            agent_session_id=None,
            agent_task_dir=context.agent_task_dir,
        )

        self.trace.emit(
            "agent.started",
            actor="agent",
            data={
                "agent_id": self.spec.id,
                "type": "pi",
                "execution": "docker",
                "image": self.spec.image,
                "network": primary_network,
                "target_network": environment_network,
                "runtime": self.spec.runtime,
                "cap_add": self.pi.cap_add,
                "model": self.pi.model,
                "provider": "harness" if gateway_enabled else self.pi.provider,
                "thinking": self.pi.thinking,
                "tools": self.pi.tools,
                "gateway_injected": gateway_enabled,
                "gateway_network": gateway_network,
                "network_profile": self.spec.network_profile,
                "network_profile_enforcement": (
                    "docker-none"
                    if self.spec.network_profile in {"offline", "fully-offline"}
                    else "docker-internal-network"
                    if self.spec.network_profile == "target-only"
                    else "docker-internal-network-gateway-proxy"
                    if self.spec.network_profile == "model-allowed"
                    else "advisory"
                    if self.spec.network_profile == "benchmark-only"
                    else "unrestricted"
                ),
            },
        )

        monitor = BudgetMonitor(event_file, task.budgets, self.trace)
        timed_out = False
        started = time.monotonic()
        offset = 0
        remainder = ""

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

            with (
                open_regular_file(raw_path, "ab") as stdout_handle,
                open_regular_file(stderr_path, "wb") as stderr_handle,
            ):
                proc = subprocess.Popen(
                    ["docker", "start", "-a", container_name],
                    cwd=task_dir,
                    env=os.environ.copy(),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                while proc.poll() is None:
                    offset, remainder = self._poll_output(
                        raw_path,
                        offset=offset,
                        remainder=remainder,
                        event_file=event_file,
                        account_model=not gateway_enabled,
                    )
                    exceeded = monitor.poll()
                    if exceeded:
                        _terminate_process(proc)
                        break
                    if (
                        time.monotonic() - started
                        > task.budgets.wall_time + FINALIZE_GRACE_SECONDS
                    ):
                        timed_out = True
                        self.trace.emit(
                            "budget.exceeded",
                            data={
                                "budget": "wall_time",
                                "limit": task.budgets.wall_time,
                                "value": time.monotonic() - started,
                            },
                        )
                        _terminate_process(proc)
                        break
                    time.sleep(0.05)
                returncode = proc.wait()
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        offset, remainder = self._poll_output(
            raw_path,
            offset=offset,
            remainder=remainder,
            event_file=event_file,
            account_model=not gateway_enabled,
        )
        del offset
        if remainder.strip():
            self._translate_line(
                remainder.strip(),
                event_file=event_file,
                account_model=not gateway_enabled,
            )
        monitor.finish()

        result = AgentResult(
            returncode=returncode,
            timed_out=timed_out,
            budget_exceeded=monitor.exceeded,
            stdout=read_regular_text(raw_path),
            stderr=read_regular_text(stderr_path),
            metrics=monitor.metrics,
        )
        self.trace.emit(
            "agent.finished",
            actor="agent",
            data={
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "budget_exceeded": result.budget_exceeded,
                "metrics": result.metrics.as_dict(),
                "pi_protocol": "json",
                "execution": "docker",
                "image": self.spec.image,
            },
        )
        return result
