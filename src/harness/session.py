from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from .agent import AgentResult
from .budget import UsageMetrics, charge_usage_event
from .secureio import open_regular_file


@dataclass(frozen=True)
class AgentEvent:
    type: str
    data: dict[str, Any]
    event_id: str | None = None


@dataclass(frozen=True)
class AgentObservation:
    type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class AgentCheckpoint:
    id: str
    event_offset: int
    feedback_offset: int
    session_id: str | None = None
    gateway_event_offset: int = 0


class AgentSession(Protocol):
    async def events(self) -> AsyncIterator[AgentEvent]: ...

    async def observe(self, observation: AgentObservation) -> None: ...

    async def checkpoint(self) -> AgentCheckpoint: ...

    async def close(self, reason: str) -> None: ...

    async def result(self) -> AgentResult: ...


class SessionPhase(StrEnum):
    """Observable lifecycle of one agent session, mirrored into the trace.

    One-shot sessions use ``created -> started -> closed``; interactive RPC
    sessions alternate ``prompting``/``settled`` per turn. The phases replace
    the private flags (``_prompt_sent``/``_settled``/``_closed``) as the
    contract surface while those flags keep driving the control flow.
    """

    CREATED = "created"
    STARTED = "started"
    PROMPTING = "prompting"
    SETTLED = "settled"
    CRASHED = "crashed"
    CLOSED = "closed"


_PHASE_TRANSITIONS: dict[SessionPhase, frozenset[SessionPhase]] = {
    SessionPhase.CREATED: frozenset(
        {SessionPhase.STARTED, SessionPhase.CRASHED, SessionPhase.CLOSED}
    ),
    SessionPhase.STARTED: frozenset(
        {SessionPhase.PROMPTING, SessionPhase.SETTLED, SessionPhase.CRASHED, SessionPhase.CLOSED}
    ),
    SessionPhase.PROMPTING: frozenset({SessionPhase.SETTLED, SessionPhase.CRASHED, SessionPhase.CLOSED}),
    SessionPhase.SETTLED: frozenset({SessionPhase.PROMPTING, SessionPhase.CRASHED, SessionPhase.CLOSED}),
    SessionPhase.CRASHED: frozenset({SessionPhase.CLOSED}),
    SessionPhase.CLOSED: frozenset(),
}


class SessionPhaseTracker:
    """Pure session phase machine; callers own the side effects.

    Same-target transitions are idempotent no-ops (an agent may settle twice
    in one stdout batch), invalid jumps raise so wiring mistakes surface in
    tests rather than as silent trace gaps.
    """

    def __init__(self, *, trace: Any = None, actor: str = "harness") -> None:
        self._phase = SessionPhase.CREATED
        self._trace = trace
        self._actor = actor

    @property
    def phase(self) -> SessionPhase:
        return self._phase

    def transition(self, target: SessionPhase, **context_data: Any) -> SessionPhase:
        if target is self._phase:
            return self._phase
        if target not in _PHASE_TRANSITIONS[self._phase]:
            raise ValueError(f"invalid session phase transition: {self._phase} -> {target}")
        previous = self._phase
        self._phase = target
        if self._trace is not None:
            self._trace.emit(
                "agent.session.phase",
                actor=self._actor,
                data={"from": previous.value, "to": target.value, **context_data},
            )
        return self._phase


@dataclass(frozen=True)
class AgentCapabilities:
    """What a session can consume and produce; degradations stay explicit.

    ``live_steer`` says whether ``observe()`` reaches a running agent turn in
    real time. One-shot sessions only append to the feedback file, which the
    agent may poll - that degradation is part of the contract, not an accident.
    """

    live_steer: bool = False
    typed_event_channel: Literal["agent_event_file", "shared_event_file", "none"] = (
        "shared_event_file"
    )
    submission_channels: tuple[str, ...] = ("world_inbox",)


ONE_SHOT_AGENT_CAPABILITIES = AgentCapabilities()
RPC_AGENT_CAPABILITIES = AgentCapabilities(
    live_steer=True,
    typed_event_channel="agent_event_file",
    submission_channels=("world_inbox", "submission_inbox"),
)



def feedback_record(observation: AgentObservation) -> str:
    """Serialize one Harness-to-Agent observation for the feedback file."""

    payload = {
        "id": f"feedback_{uuid.uuid4().hex}",
        "type": observation.type,
        "data": observation.data,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def control_record(reason: str) -> str:
    """Serialize the session shutdown record written to the control file."""

    return (
        json.dumps(
            {"type": "session.close_requested", "data": {"reason": reason}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def append_feedback(path: Path, line: str) -> int:
    """Append one feedback line without following symlinks; returns the new offset."""

    with open_regular_file(path, "a") as handle:
        handle.write(line)
        handle.flush()
        return handle.tell()


def append_control(path: Path, line: str) -> None:
    with open_regular_file(path, "a") as handle:
        handle.write(line)
        handle.flush()


class OneShotAgentSession:
    """Compatibility session that streams normalized events around a legacy adapter.run()."""

    def __init__(
        self,
        adapter: Any,
        *,
        context: Any,
        poll_interval: float = 0.05,
    ) -> None:
        self.adapter = adapter
        self.context = context
        self.run_dir = Path(context.run_dir)
        self.poll_interval = poll_interval
        self.event_path = self.run_dir / "events.jsonl"
        self.feedback_path = self.run_dir / "agent.feedback.jsonl"
        self.control_path = self.run_dir / "agent.control.jsonl"
        self._task: asyncio.Task[AgentResult] | None = None
        self._event_offset = 0
        self._event_line = 0
        self._feedback_offset = 0
        self._closed = False
        self._usage_metrics = UsageMetrics()
        self._usage_telemetry_valid = True
        self.phase = SessionPhaseTracker(trace=getattr(adapter, "trace", None))

    @property
    def capabilities(self) -> AgentCapabilities:
        return ONE_SHOT_AGENT_CAPABILITIES

    @property
    def usage_metrics(self) -> dict[str, int | float]:
        return self._usage_metrics.as_dict()

    @property
    def usage_telemetry_valid(self) -> bool:
        return self._usage_telemetry_valid

    def _account(self, event: AgentEvent) -> None:
        if not charge_usage_event(self._usage_metrics, event.type, event.data):
            self._usage_telemetry_valid = False

    @classmethod
    async def start(
        cls,
        adapter: Any,
        *,
        context: Any,
    ) -> OneShotAgentSession:
        session = cls(adapter, context=context)
        session.feedback_path.touch(exist_ok=True)
        session.control_path.touch(exist_ok=True)
        session._task = asyncio.create_task(asyncio.to_thread(adapter.run, context))
        session.phase.transition(SessionPhase.STARTED)
        return session

    async def events(self) -> AsyncIterator[AgentEvent]:
        if self._task is None:
            raise RuntimeError("session has not been started")

        while True:
            event = None
            if self.event_path.exists():
                with open_regular_file(
                    self.event_path, "r", encoding="utf-8", errors="replace"
                ) as handle:
                    handle.seek(self._event_offset)
                    line = handle.readline()
                    if line and (line.endswith(("\n", "\r")) or self._task.done()):
                        # Advance only through the event yielded below. A read-ahead
                        # chunk could contain later events that have not been applied.
                        self._event_offset = handle.tell()
                        self._event_line += 1
                        event = self._parse_event(
                            line,
                            fallback_event_id=f"agent-event-line:{self._event_line}",
                        )

            if event is not None:
                self._account(event)
                yield event
                continue

            if self._task.done():
                await self._task
                break
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _parse_event(
        line: str,
        *,
        fallback_event_id: str | None = None,
    ) -> AgentEvent | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return AgentEvent(
                type="agent.telemetry_error",
                data={"message": "invalid JSON event", "line": line[:500]},
                event_id=fallback_event_id,
            )
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        event_id = payload.get("event_id")
        return AgentEvent(
            type=str(payload.get("type", "unknown")),
            data=data if isinstance(data, dict) else {},
            event_id=(event_id if isinstance(event_id, str) and event_id else fallback_event_id),
        )

    async def observe(self, observation: AgentObservation) -> None:
        self._feedback_offset = await asyncio.to_thread(
            append_feedback, self.feedback_path, feedback_record(observation)
        )

    async def checkpoint(self) -> AgentCheckpoint:
        return AgentCheckpoint(
            id=f"checkpoint_{uuid.uuid4().hex}",
            event_offset=self._event_offset,
            feedback_offset=self._feedback_offset,
        )

    async def close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.phase.transition(SessionPhase.CLOSED, reason=reason)
        await asyncio.to_thread(append_control, self.control_path, control_record(reason))

    async def result(self) -> AgentResult:
        if self._task is None:
            raise RuntimeError("session has not been started")
        return await self._task
