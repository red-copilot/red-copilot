"""Spike-local goal state machine, arbitrated by the runtime.

The runtime owns the goal store: it seeds one root goal per challenge, accepts
subgoal proposals from the solvers, and is the only writer of ``goal.jsonl``.
A solver cannot close a subgoal by asserting it - ``achieved`` requires a fact
that the merged blackboard already holds and that a *different* session
contributed first. That rule is what makes cross-solver communication
load-bearing rather than decorative.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .blackboard import FactEntry, normalise
from .models import MAX_DESCRIPTION_CHARS, MAX_SUBGOALS

PENDING = "pending"
ACTIVE = "active"
ACHIEVED = "achieved"
BLOCKED = "blocked"
ABANDONED = "abandoned"

TERMINAL_STATUSES = frozenset({ACHIEVED, ABANDONED})
SOLVER_STATUSES = (ACTIVE, BLOCKED, ACHIEVED, ABANDONED)
RUNTIME_ACTOR = "runtime"

_GOAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

# Rejection reasons; recorded verbatim so the operator can read the transcript.
REASON_UNKNOWN_OP = "unknown_op"
REASON_MALFORMED = "malformed_op"
REASON_UNKNOWN_SESSION = "unknown_session"
REASON_BAD_ID = "bad_goal_id"
REASON_DUPLICATE = "duplicate_goal_id"
REASON_UNKNOWN_PARENT = "unknown_parent"
REASON_EMPTY_DESCRIPTION = "empty_description"
REASON_SUBGOAL_CAP = "subgoal_cap"
REASON_UNKNOWN_GOAL = "unknown_goal"
REASON_BAD_STATUS = "bad_status"
REASON_TERMINAL = "terminal_status"
REASON_UNKNOWN_EVIDENCE = "unknown_evidence_fact"
REASON_UNCORROBORATED = "uncorroborated"


@dataclass
class GoalNode:
    id: str
    description: str
    status: str
    parent_id: str | None
    owner: str
    reason: str = ""
    evidence_fact: str = ""
    created_round: int = 0
    updated_round: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "parent_id": self.parent_id,
            "owner": self.owner,
            "reason": self.reason,
            "evidence_fact": self.evidence_fact,
            "created_round": self.created_round,
            "updated_round": self.updated_round,
        }


@dataclass(frozen=True)
class GoalOpRead:
    session: str
    round: int
    raw: str


@dataclass(frozen=True)
class GoalOp:
    op: str
    session: str
    round: int
    goal_id: str = ""
    parent: str = ""
    description: str = ""
    status: str = ""
    reason: str = ""
    evidence_fact: str = ""


@dataclass(frozen=True)
class GoalOpResult:
    session: str
    round: int
    op: str
    goal_id: str
    result: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "session": self.session,
            "round": self.round,
            "op": self.op,
            "goal_id": self.goal_id,
            "result": self.result,
            "reason": self.reason,
        }


def parse_goal_op(*, session: str, round: int, raw: str) -> GoalOp:
    """Parse one goal-op line; malformed input becomes an explicit rejection."""

    text = raw.strip()
    if not text:
        return GoalOp(op="", session=session, round=round)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return GoalOp(op=REASON_MALFORMED, session=session, round=round)
    if not isinstance(payload, dict):
        return GoalOp(op=REASON_MALFORMED, session=session, round=round)
    parent = payload.get("parent")
    return GoalOp(
        op=str(payload.get("op", "")).strip().lower(),
        session=session,
        round=round,
        goal_id=str(payload.get("id", "")).strip(),
        parent=str(parent).strip() if isinstance(parent, str) else "",
        description=str(payload.get("description", "")).strip(),
        status=str(payload.get("status", "")).strip().lower(),
        reason=str(payload.get("reason", "")).strip(),
        evidence_fact=str(payload.get("evidence_fact", "")).strip(),
    )


@dataclass
class GoalStore:
    """Append-only goal journal plus the derived node map."""

    code: str
    description: str
    journal: Path | None = None
    nodes: dict[str, GoalNode] = field(default_factory=dict)
    history: list[GoalOpResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        root = GoalNode(
            id=self.root_id,
            description=self.description or f"challenge {self.code}",
            status=PENDING,
            parent_id=None,
            owner=RUNTIME_ACTOR,
        )
        self.nodes[root.id] = root
        if self.journal is not None and self.journal.exists():
            self._replay()

    @property
    def root_id(self) -> str:
        return f"goal:{self.code}"

    @property
    def root(self) -> GoalNode:
        return self.nodes[self.root_id]

    @property
    def subgoals(self) -> list[GoalNode]:
        return sorted(
            (node for node in self.nodes.values() if node.id != self.root_id),
            key=lambda node: (node.created_round, node.id),
        )

    # -- runtime-only root transitions ------------------------------------
    def activate_root(self, *, round: int) -> None:
        self._apply_runtime(self.root, ACTIVE, round=round)

    def achieve_root(self, *, flag: str, round: int) -> None:
        node = self.root
        node.evidence_fact = flag
        self._apply_runtime(node, ACHIEVED, round=round, reason="platform_accepted_flag")

    def abandon_root(self, *, reason: str, round: int) -> None:
        self._apply_runtime(self.root, ABANDONED, round=round, reason=reason)

    def block_root(self, *, reason: str, round: int) -> None:
        self._apply_runtime(self.root, BLOCKED, round=round, reason=reason)

    def _apply_runtime(
        self,
        node: GoalNode,
        status: str,
        *,
        round: int,
        reason: str = "",
    ) -> None:
        if node.status in TERMINAL_STATUSES:
            return
        node.status = status
        if reason:
            node.reason = reason
        node.updated_round = round
        self._record(
            {
                "round": round,
                "session": RUNTIME_ACTOR,
                "op": "runtime_status",
                "goal_id": node.id,
                "result": "applied",
                "reason": reason,
                "goal": node.as_dict(),
            }
        )

    # -- solver-proposed subgoals -----------------------------------------
    def apply_reads(
        self,
        reads: Sequence[GoalOpRead],
        *,
        round: int,
        facts: Iterable[FactEntry],
        sessions: Sequence[str] = (),
    ) -> list[GoalOpResult]:
        fact_index: Mapping[str, FactEntry] = {normalise(fact.text): fact for fact in facts}
        known_sessions = set(sessions) or None
        results: list[GoalOpResult] = []
        for read in sorted(reads, key=lambda item: item.session):
            for raw in read.raw.splitlines():
                if not raw.strip():
                    continue
                parsed = parse_goal_op(session=read.session, round=round, raw=raw)
                results.append(
                    self._dispatch(
                        parsed,
                        round=round,
                        fact_index=fact_index,
                        known_sessions=known_sessions,
                    )
                )
        return results

    def _dispatch(
        self,
        op: GoalOp,
        *,
        round: int,
        fact_index: Mapping[str, FactEntry],
        known_sessions: set[str] | None,
    ) -> GoalOpResult:
        if known_sessions is not None and op.session not in known_sessions:
            return self._reject(op, round, REASON_UNKNOWN_SESSION)
        if op.op == "propose":
            return self._propose(op, round, known_sessions)
        if op.op == "status":
            return self._status(op, round, fact_index)
        if op.op in ("", REASON_MALFORMED):
            return self._reject(op, round, REASON_MALFORMED)
        return self._reject(op, round, REASON_UNKNOWN_OP)

    def _propose(
        self,
        op: GoalOp,
        round: int,
        known_sessions: set[str] | None,
    ) -> GoalOpResult:
        del known_sessions
        if not _GOAL_ID.match(op.goal_id) or op.goal_id == self.root_id:
            return self._reject(op, round, REASON_BAD_ID)
        if op.goal_id in self.nodes:
            return self._reject(op, round, REASON_DUPLICATE)
        parent_id = op.parent or self.root_id
        if parent_id not in self.nodes:
            return self._reject(op, round, REASON_UNKNOWN_PARENT)
        if not op.description:
            return self._reject(op, round, REASON_EMPTY_DESCRIPTION)
        if len(self.subgoals) >= MAX_SUBGOALS:
            return self._reject(op, round, REASON_SUBGOAL_CAP)
        node = GoalNode(
            id=op.goal_id,
            description=op.description[:MAX_DESCRIPTION_CHARS],
            status=ACTIVE,
            parent_id=parent_id,
            owner=op.session,
            created_round=round,
            updated_round=round,
        )
        self.nodes[node.id] = node
        return self._accept(op, round, node, reason="")

    def _status(
        self,
        op: GoalOp,
        round: int,
        fact_index: Mapping[str, FactEntry],
    ) -> GoalOpResult:
        node = self.nodes.get(op.goal_id)
        if node is None or node.id == self.root_id:
            return self._reject(op, round, REASON_UNKNOWN_GOAL)
        if op.status not in SOLVER_STATUSES:
            return self._reject(op, round, REASON_BAD_STATUS)
        if node.status in TERMINAL_STATUSES:
            return self._reject(op, round, REASON_TERMINAL)
        if op.status == ACHIEVED:
            if not op.evidence_fact:
                return self._reject(op, round, REASON_UNKNOWN_EVIDENCE, node=node)
            fact = fact_index.get(normalise(op.evidence_fact))
            if fact is None:
                return self._reject(op, round, REASON_UNKNOWN_EVIDENCE, node=node)
            if fact.first_session == op.session:
                return self._reject(op, round, REASON_UNCORROBORATED, node=node)
            node.evidence_fact = op.evidence_fact
        node.status = op.status
        node.reason = op.reason if op.status == BLOCKED else ""
        node.updated_round = round
        return self._accept(op, round, node, reason="")

    def _accept(
        self,
        op: GoalOp,
        round: int,
        node: GoalNode,
        *,
        reason: str,
    ) -> GoalOpResult:
        result = GoalOpResult(
            session=op.session,
            round=round,
            op=op.op,
            goal_id=node.id,
            result="applied",
            reason=reason,
        )
        self.history.append(result)
        self._record({**result.as_dict(), "goal": node.as_dict()})
        return result

    def _reject(
        self,
        op: GoalOp,
        round: int,
        reason: str,
        *,
        node: GoalNode | None = None,
    ) -> GoalOpResult:
        if node is not None and reason in (REASON_UNCORROBORATED, REASON_UNKNOWN_EVIDENCE):
            # The solver tried to close a goal it cannot close; park the goal
            # instead of leaving it looking active.
            node.status = BLOCKED
            node.reason = reason
            node.updated_round = round
        result = GoalOpResult(
            session=op.session,
            round=round,
            op=op.op or REASON_MALFORMED,
            goal_id=op.goal_id,
            result="rejected",
            reason=reason,
        )
        self.history.append(result)
        record: dict[str, object] = {**result.as_dict()}
        if node is not None and reason in (REASON_UNCORROBORATED, REASON_UNKNOWN_EVIDENCE):
            record["goal"] = node.as_dict()
        self._record(record)
        return result

    # -- rendering and persistence ----------------------------------------
    def render_lines(self) -> list[str]:
        lines = [
            f"- {self.root.id} [{self.root.status}] {self.root.description}"
            + (f" (reason: {self.root.reason})" if self.root.reason else "")
        ]
        for node in self.subgoals:
            suffix = f" (reason: {node.reason})" if node.reason else ""
            lines.append(
                f"- {node.id} [{node.status}] {node.description} "
                f"(by {node.owner}, round {node.created_round}){suffix}"
            )
        return lines

    def summary(self) -> dict[str, object]:
        return {
            "root": self.root.as_dict(),
            "subgoals": [node.as_dict() for node in self.subgoals],
            "counts": {
                "subgoals": len(self.subgoals),
                "active": sum(1 for node in self.subgoals if node.status == ACTIVE),
                "achieved": sum(1 for node in self.subgoals if node.status == ACHIEVED),
                "blocked": sum(1 for node in self.subgoals if node.status == BLOCKED),
                "rejected_ops": sum(1 for item in self.history if item.result == "rejected"),
            },
        }

    def _replay(self) -> None:
        assert self.journal is not None
        for raw in self.journal.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("result") != "applied":
                continue
            payload = record.get("goal")
            if not isinstance(payload, dict) or "id" not in payload:
                continue
            self.nodes[str(payload["id"])] = GoalNode(
                id=str(payload["id"]),
                description=str(payload.get("description", "")),
                status=str(payload.get("status", PENDING)),
                parent_id=payload.get("parent_id"),  # type: ignore[arg-type]
                owner=str(payload.get("owner", RUNTIME_ACTOR)),
                reason=str(payload.get("reason", "")),
                evidence_fact=str(payload.get("evidence_fact", "")),
                created_round=int(payload.get("created_round", 0) or 0),
                updated_round=int(payload.get("updated_round", 0) or 0),
            )

    def _record(self, record: dict[str, object]) -> None:
        if self.journal is None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
