from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from .agent import AgentResult
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

    RPC sessions alternate ``prompting``/``settled`` per turn. The phases
    replace the private flags (``_prompt_sent``/``_settled``/``_closed``) as
    the contract surface while those flags keep driving the control flow.
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
    real time. The Pi RPC session always advertises live steering and its
    dedicated typed-event file.
    """

    live_steer: bool = False
    typed_event_channel: Literal["agent_event_file", "shared_event_file", "none"] = (
        "shared_event_file"
    )
    submission_channels: tuple[str, ...] = ("world_inbox",)


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
