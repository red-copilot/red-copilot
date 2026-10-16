from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from .aci import TypedACI
from .action_verifier import ActionVerifier
from .hints import HINTS_FILE, HintCursor, HintRecord
from .progress import ProgressLedger
from .session import AgentEvent, AgentObservation, AgentSession
from .solver_policy import (
    DEFAULT_MAX_NO_PROGRESS,
    DEFAULT_MAX_UNCONFIRMED_PROGRESS,
    SolverAction,
    SolverDecision,
    decide_solver_action,
)
from .trace import TraceRecorder
from .verification import AuthorizedActionVerdict, VerifierRegistry
from .world import (
    Observation,
    Provenance,
    WorldContextBuilder,
    WorldRepository,
)
from .world.live import WorldInboxCursor

MAX_PROCESSED_EVENT_IDS = 50_000

# The finalize steer asks for a report with these four sections. The Agent's
# summary is untrusted text, so the Harness only *reports* what is missing; it
# never fails a run because a heading is absent. The generic English markers are
# matched as whole words: "confirmed" is a substring of "unconfirmed", so a
# plain membership test would count one section's heading for another.
SUMMARY_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("confirmed", ("已确认事实", "confirmed fact", "confirmed")),
    ("unconfirmed", ("未确认假设", "unconfirmed", "hypothes")),
    ("ruled out", ("已尝试并排除", "ruled out", "tried and")),
    ("unfinished", ("未完成意图", "unfinished", "open intent")),
)
MAX_SUMMARY_BYTES = 256 * 1024

# The carry-forward brief replaces a long transcript with the short version of
# the same state. It is deliberately small: it must fit in any context.
CONTEXT_BRIEF_MAX_CHARS = 8000
CONTEXT_BRIEF_FACTS = 12
CONTEXT_BRIEF_RULED_OUT = 8
CONTEXT_BRIEF_FAILURES = 5


def _mentions_summary_marker(text: str, marker: str) -> bool:
    """True when a marker appears as its own word rather than inside a longer one."""
    return re.search(rf"(?<![a-z0-9]){re.escape(marker)}", text) is not None


def missing_summary_sections(summary_path: Path) -> list[str]:
    """Return the report sections the Agent did not write.

    A missing or unreadable summary reports every section as missing; the
    caller decides whether that is worth a trace event.
    """
    try:
        if summary_path.stat().st_size > MAX_SUMMARY_BYTES:
            return [name for name, _ in SUMMARY_SECTIONS]
        text = summary_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return [name for name, _ in SUMMARY_SECTIONS]
    missing: list[str] = []
    for name, markers in SUMMARY_SECTIONS:
        if not any(_mentions_summary_marker(text, marker.lower()) for marker in markers):
            missing.append(name)
    return missing


@dataclass
class SolverLoopStats:
    aci_accepted: int = 0
    aci_rejected: int = 0
    aci_world_mutations: int = 0
    inbox_accepted: int = 0
    inbox_rejected: int = 0
    duplicate_events: int = 0
    conflicting_event_ids: int = 0
    last_verification_key: tuple | None = None


class SolverLoop:
    """Shared state/verification loop for interactive AgentSession execution."""

    VERIFY_EVENTS: ClassVar[frozenset[str]] = frozenset(
        {"tool.result", "progress.updated", "world.observe"}
    )
    # Remaining-budget fractions at which the Agent is steered once to converge.
    BUDGET_STEER_THRESHOLDS: ClassVar[tuple[float, ...]] = (0.5, 0.25, 0.1)

    def __init__(
        self,
        *,
        world: WorldRepository,
        progress: ProgressLedger,
        run_dir: Path,
        trace: TraceRecorder,
        actor: str,
        context_query: str,
        planned_world_revision: int,
        typed_aci: TypedACI | None = None,
        action_verifier: ActionVerifier | None = None,
        verifier_registry: VerifierRegistry | None = None,
        context_builder: WorldContextBuilder | None = None,
        world_inbox: WorldInboxCursor | None = None,
        max_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
        max_unconfirmed_progress: int = DEFAULT_MAX_UNCONFIRMED_PROGRESS,
        hint_request: Callable[[], Awaitable[str | None]] | None = None,
        hint_threshold: int = 0,
        processed_event_ids: dict[str, str] | None = None,
        world_inbox_path: Path | None = None,
        hint_cursor: HintCursor | None = None,
        sync_agent_views: Callable[[], None] | None = None,
        summary_path: Path | None = None,
        notes_dir: Path | None = None,
    ) -> None:
        self.world = world
        self.progress = progress
        self.run_dir = run_dir
        self.trace = trace
        self.actor = actor
        self.context_query = context_query
        self.planned_world_revision = planned_world_revision
        self.typed_aci = typed_aci or TypedACI()
        self.verifier_registry = verifier_registry or VerifierRegistry()
        self.action_verifier = action_verifier or ActionVerifier(self.verifier_registry)
        self.context_builder = context_builder or WorldContextBuilder()
        self.world_inbox = world_inbox or WorldInboxCursor(
            world_inbox_path or run_dir / "world.inbox.jsonl",
            world,
            actor=actor,
        )
        self.hint_cursor = hint_cursor or HintCursor(run_dir / HINTS_FILE)
        # The cursor offset is not checkpointed, so a resumed loop would re-read
        # every previously delivered hint. The World's persisted human.hint
        # observations are the durable delivery ledger: seed the delivered set
        # from them so hints are steered exactly once per run.
        self._delivered_hint_ids: set[str] = {
            record_id.removeprefix("human.hint:")
            for record_id in world.snapshot.observations
            if record_id.startswith("human.hint:")
        }
        self.progress_path = run_dir / "progress.json"
        self.summary_path = summary_path or (run_dir / "agent.summary.md")
        self.notes_dir = notes_dir
        self.sync_agent_views = sync_agent_views
        self.max_no_progress = max_no_progress
        self.max_unconfirmed_progress = max_unconfirmed_progress
        self._hint_request = hint_request
        self.hint_threshold = hint_threshold
        self._hint_steered = False
        self._budget_steers_fired: set[float] = set()
        self._finalize_steered = False
        self._finalize_prompted = False
        self.stats = SolverLoopStats()
        self._processed_event_ids: dict[str, str] = dict(processed_event_ids or {})

    def _correlate_legacy_tool_event(self, event: AgentEvent) -> AgentEvent:
        if event.type not in {"tool.call", "tool.result"}:
            return event
        data = dict(event.data)
        event_id = event.event_id
        call_id = data.get("tool_call_id")
        valid_call_id = isinstance(call_id, str) and 0 < len(call_id) <= 256
        correlations = self.progress.tool_event_correlations
        if not valid_call_id and isinstance(event_id, str):
            call_id = correlations.get(event_id)
            valid_call_id = isinstance(call_id, str) and 0 < len(call_id) <= 256

        if not valid_call_id and event.type == "tool.call":
            if isinstance(event_id, str) and event_id:
                seed = event_id.encode("utf-8")
                call_id = f"legacy_{hashlib.sha256(seed).hexdigest()[:32]}"
            else:
                call_id = f"legacy_{uuid.uuid4().hex}"
            valid_call_id = True
        elif not valid_call_id and event.type == "tool.result":
            tool = data.get("tool")
            candidates = [
                action
                for action in self.progress.active_actions.values()
                if not isinstance(tool, str) or action.get("tool") == tool
            ]
            if len(candidates) == 1:
                candidate_id = candidates[0].get("tool_call_id")
                if isinstance(candidate_id, str) and candidate_id:
                    call_id = candidate_id
                    valid_call_id = True

        if valid_call_id:
            data["tool_call_id"] = call_id
            if isinstance(event_id, str) and event_id:
                correlations[event_id] = call_id
                if len(correlations) > 100_000:
                    correlations.pop(next(iter(correlations)))
        return AgentEvent(type=event.type, data=data, event_id=event.event_id)

    async def process_event(
        self,
        session: AgentSession,
        event: AgentEvent,
        *,
        trusted_verdict: AuthorizedActionVerdict | None = None,
    ) -> SolverDecision:
        event = self._correlate_legacy_tool_event(event)
        if event.event_id:
            canonical = json.dumps(
                {"type": event.type, "data": event.data},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            previous = self._processed_event_ids.get(event.event_id)
            if previous == fingerprint:
                self.stats.duplicate_events += 1
                decision = SolverDecision(SolverAction.CONTINUE, "duplicate_event")
                self._emit_decision(decision, event_type=event.type, event_id=event.event_id)
                return decision
            if previous is not None:
                self.stats.conflicting_event_ids += 1
                self.trace.emit(
                    "event.id_conflict",
                    actor="harness",
                    data={"event_id": event.event_id, "event_type": event.type},
                )
                await session.observe(
                    AgentObservation(
                        type="event.id_conflict",
                        data={"event_id": event.event_id, "event_type": event.type},
                    )
                )
                decision = SolverDecision(SolverAction.CONTINUE, "conflicting_event_id")
                self._emit_decision(decision, event_type=event.type, event_id=event.event_id)
                return decision
            self._processed_event_ids[event.event_id] = fingerprint
            if len(self._processed_event_ids) > MAX_PROCESSED_EVENT_IDS:
                self._processed_event_ids.pop(next(iter(self._processed_event_ids)))

        self.progress.record_event(event)
        self.progress.write(self.progress_path)
        self._sync_agent_views()
        before_revision = self.world.revision
        if event.type in {"tool.call", "tool.result"}:
            tool_call_id = event.data.get("tool_call_id")
            if isinstance(tool_call_id, str) and 0 < len(tool_call_id) <= 256:
                trace_data: dict[str, object] = {
                    "tool_call_id": tool_call_id,
                    "source_event_id": (
                        event.event_id
                        if isinstance(event.event_id, str) and len(event.event_id) <= 256
                        else None
                    ),
                }
                if event.type == "tool.result":
                    trace_data["is_error"] = bool(event.data.get("is_error", False))
                    trace_data["execution_status"] = (self.progress.last_action or {}).get("status")
                self.trace.emit(
                    event.type,
                    actor=self.actor,
                    data=trace_data,
                    parent_event_id=event.event_id,
                    action_id=tool_call_id,
                )

        try:
            aci_mutations = self.typed_aci.apply(
                event,
                world=self.world,
                progress=self.progress,
                actor=self.actor,
            )
        except (KeyError, TypeError, ValueError) as exc:
            aci_mutations = 0
            self.stats.aci_rejected += 1
            self.trace.emit(
                "aci.rejected",
                actor="harness",
                data={
                    "event_type": event.type,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            await session.observe(
                AgentObservation(
                    type="aci.feedback",
                    data={
                        "accepted": False,
                        "event_type": event.type,
                        "message": str(exc),
                    },
                )
            )
        else:
            if event.type.startswith(("world.", "action.")):
                self.stats.aci_accepted += 1
                await session.observe(
                    AgentObservation(
                        type="aci.feedback",
                        data={
                            "accepted": True,
                            "event_type": event.type,
                            "world_mutations": aci_mutations,
                        },
                    )
                )
            self.stats.aci_world_mutations += aci_mutations
            if aci_mutations:
                self.trace.emit(
                    "aci.applied",
                    actor="harness",
                    data={
                        "event_type": event.type,
                        "mutations": aci_mutations,
                        "revision_before": before_revision,
                        "revision_after": self.world.revision,
                    },
                )

        live_ingest = self.world_inbox.poll()
        self.stats.inbox_accepted += live_ingest.accepted
        self.stats.inbox_rejected += live_ingest.rejected
        # Operator hints: advisory guidance from a human operator, recorded at
        # evidence tier so the Agent and the end-of-run summary see it. A hint
        # is never an objective verdict and never verifies anything by itself.
        for hint in self.hint_cursor.poll():
            if not self._absorb_operator_hint(hint):
                continue
            await session.observe(
                AgentObservation(
                    type="solver.hint",
                    data={
                        "source": "operator",
                        "id": hint.id,
                        "text": hint.text,
                        "message": f"Operator hint ({hint.id}): {hint.text}",
                    },
                )
            )
        after_revision = self.world.revision
        if live_ingest.accepted or live_ingest.rejected:
            self.trace.emit(
                "world.ingested",
                data={
                    "accepted": live_ingest.accepted,
                    "rejected": live_ingest.rejected,
                    "errors": [error.model_dump() for error in live_ingest.errors[:10]],
                    "revision_before": before_revision,
                    "revision_after": after_revision,
                    "live": True,
                },
            )

        if after_revision != before_revision:
            self._write_context()
            self.trace.emit(
                "world.state.updated",
                data={
                    "revision_before": before_revision,
                    "revision_after": after_revision,
                },
            )
            await session.observe(
                AgentObservation(
                    type="world.state.updated",
                    data={
                        "revision_before": before_revision,
                        "revision_after": after_revision,
                        "context_path": "world.context.txt",
                    },
                )
            )

        if event.type in self.VERIFY_EVENTS:
            verification = self.action_verifier.verify(
                self.progress,
                run_id=self.trace.run_id,
                planned_world_revision=self.planned_world_revision,
                current_world_revision=after_revision,
                evidence_world_revision=before_revision,
                trusted_verdict=trusted_verdict,
            )
            if verification is None:
                self.progress.skipped_verifications += 1
            else:
                verification_key = (
                    verification.status,
                    verification.expected_observation,
                    verification.actual_observation,
                    tuple(verification.replan_reasons),
                    (self.progress.last_action or {}).get("tool_call_id"),
                    (self.progress.last_action or {}).get("status"),
                )
                if verification_key != self.stats.last_verification_key:
                    self.stats.last_verification_key = verification_key
                    self.progress.record_verification(verification)
                    if verification.status == "verified":
                        action_id = (self.progress.last_action or {}).get("tool_call_id")
                        if isinstance(action_id, str) and action_id:
                            revision_before_verification = self.world.revision
                            self.world.upsert(
                                "observation",
                                Observation(
                                    id=f"verified-action:{action_id}",
                                    type="action.verified",
                                    content={
                                        "action_id": action_id,
                                        "expected_observation": verification.expected_observation,
                                        "actual_observation": verification.actual_observation,
                                        "evidence": [
                                            ref.model_dump(mode="json")
                                            for ref in verification.evidence
                                        ],
                                    },
                                    confidence=1.0,
                                    source="harness.action_verifier",
                                    provenance=Provenance(epistemic_status="verified"),
                                ),
                                actor="harness",
                                source_event_id=event.event_id,
                            )
                            self._write_context()
                            self.trace.emit(
                                "world.state.updated",
                                data={
                                    "revision_before": revision_before_verification,
                                    "revision_after": self.world.revision,
                                    "cause": "action.verified",
                                },
                            )
                    self.trace.emit(
                        "action.verified",
                        actor="harness",
                        data=verification.model_dump(mode="json"),
                    )
                    await session.observe(
                        AgentObservation(
                            type="solver.verification",
                            data=verification.model_dump(mode="json"),
                        )
                    )

        self.progress.write(self.progress_path)
        self._sync_agent_views()
        await self._maybe_steer_repetition(session)
        await self._maybe_steer_hint(session)
        await self._maybe_steer_budget(session)
        await self._maybe_steer_finalize(session)
        await self._maybe_reask_finalize(session, event_type=event.type)
        decision = self.decide(event_type=event.type)
        decision = self._hold_stop_for_budget_grace(decision)
        if decision.action is SolverAction.STOP:
            await session.observe(
                AgentObservation(
                    type="solver.stop",
                    data={"reason": decision.reason},
                )
            )
        self._emit_decision(decision, event_type=event.type, event_id=event.event_id)
        return decision

    def _emit_decision(
        self, decision: SolverDecision, *, event_type: str, event_id: str | None
    ) -> None:
        self.trace.emit(
            "solver.decision",
            actor="harness",
            data={
                "action": decision.action.value,
                "reason": decision.reason,
                "event_type": event_type,
                "event_id": event_id,
                "world_revision": self.world.revision,
                "no_progress_count": self.progress.no_progress_count,
                "remaining_budget_fraction": self.progress.remaining_budget_fraction,
            },
        )

    @property
    def processed_event_ids(self) -> dict[str, str]:
        return dict(self._processed_event_ids)

    async def _maybe_steer_budget(self, session: AgentSession) -> None:
        """Steer the Agent once per budget threshold so it converges in time."""
        if self.progress.objective_completed:
            return
        fractions = self.progress.budget_fractions()
        if not fractions:
            return
        remaining = min(fractions.values())
        for threshold in self.BUDGET_STEER_THRESHOLDS:
            if remaining > threshold or threshold in self._budget_steers_fired:
                continue
            self._budget_steers_fired.add(threshold)
            tightest = min(fractions, key=lambda key: fractions[key])
            data = {
                "remaining_budget_fraction": remaining,
                "threshold": threshold,
                "tightest_budget": tightest,
                "budget_fractions": fractions,
            }
            self.trace.emit("solver.budget", actor="harness", data=data)
            await session.observe(
                AgentObservation(
                    type="solver.budget",
                    data={
                        **data,
                        "message": (
                            "Budget is running out. Consolidate what is already "
                            "verified and submit the candidates you actually hold; "
                            "do not start broad new exploration."
                        ),
                    },
                )
            )

    async def _maybe_steer_repetition(self, session: AgentSession) -> None:
        """Intervene once when one command family is being ground down.

        The family comes from the adapter's coarse tool identity, so "the same
        loop with a different wordlist" counts as one family. Level 1 nudges the
        Agent to change its hypothesis; level 2 asks it to converge and record
        what the path ruled out. Neither level kills the session.
        """
        if self.progress.objective_completed:
            return
        action = self.progress.repetition_action()
        if action is None:
            return
        family, streak, level = action
        self.progress.note_repetition_steer(family, level=level)
        self.trace.emit(
            "solver.repetition_steer",
            actor="harness",
            data={
                "family": family,
                "consecutive": streak,
                "level": level,
                "escalated": level > 1,
            },
        )
        if level > 1:
            message = (
                f"{streak} calls in a row share the command family {family} without "
                "confirmed progress. Stop repeating it: consolidate what is already "
                "verified, record what this path ruled out, then either widen or "
                "write your summary."
            )
        else:
            message = (
                f"{streak} calls in a row share the command family {family} without "
                "confirmed progress. Change the hypothesis or the input class before "
                "calling it again; the same command with new arguments is the same path."
            )
        await session.observe(
            AgentObservation(
                type="solver.repetition_steer",
                data={
                    "family": family,
                    "consecutive": streak,
                    "level": level,
                    "message": message,
                },
            )
        )

    async def _maybe_steer_hint(self, session: AgentSession) -> None:
        """Fetch the platform hint once when the run is stuck past the threshold.

        The callback is the benchmark adapter's one-shot fetch, so a failed
        lookup is recorded and never retried in a loop.
        """
        if (
            self._hint_steered
            or self._hint_request is None
            or self.hint_threshold <= 0
            or self.progress.objective_completed
        ):
            return
        if self.progress.no_progress_count < self.hint_threshold:
            return
        self._hint_steered = True
        hint: str | None = None
        try:
            hint = await self._hint_request()
        except Exception:  # noqa: BLE001 - a failed hint must not stall the loop
            hint = None
        self.trace.emit(
            "solver.hint",
            actor="harness",
            data={
                "source": "platform",
                "used": bool(hint),
                "threshold": self.hint_threshold,
                "no_progress_count": self.progress.no_progress_count,
            },
        )
        if not hint:
            return
        await session.observe(
            AgentObservation(
                type="solver.hint",
                data={
                    "source": "platform",
                    "threshold": self.hint_threshold,
                    "message": (
                        "Platform hint (viewing it reduces this flag's score):\n" + hint
                    ),
                },
            )
        )

    async def _maybe_steer_finalize(self, session: AgentSession) -> None:
        """Stop the Agent exploring at a soft limit and ask for a summary.

        The run is not cut off: the wall-clock deadline and every declared
        usage ceiling are soft, and the remaining grace window belongs to
        writing down what was found (facts) and what was being attempted
        (intents) so a timed-out or budget-exhausted run still leaves usable
        state.
        """
        if self._finalize_steered or self.progress.objective_completed:
            return
        exceeded = self.progress.budget_exceeded_dimension
        if exceeded is None:
            return
        if exceeded != "wall_time" and self.progress.budget_exceeded_at is None:
            # Stamp when a usage dimension first crossed so the budget STOP can
            # be held for the grace window counted from here.
            self.progress.budget_exceeded_at = datetime.now(UTC)
        soft_usage = exceeded != "wall_time"
        data: dict = {"reason": "soft_budget_limit" if soft_usage else "soft_time_limit"}
        if soft_usage:
            data["budget"] = exceeded
        self._finalize_steered = True
        self.trace.emit("solver.finalize", actor="harness", data=data)
        lead = "A run budget is exhausted" if soft_usage else "Time is up"
        await session.observe(
            AgentObservation(
                type="solver.finalize",
                data={
                    **data,
                    "summary_file": "agent.summary.md",
                    "message": (
                        f"{lead}, and this finalize instruction overrides any earlier "
                        "instruction that told you to keep working. Stop launching new "
                        "probes and do not run any more commands. Based only on what you "
                        "have already confirmed, write a short report to "
                        "$HARNESS_SUMMARY_FILE with exactly these four sections: "
                        "(1) confirmed facts - each with the evidence that confirms it; "
                        "(2) unconfirmed hypotheses; (3) what you tried and ruled out; "
                        "(4) unfinished intents. Then stop."
                    ),
                },
            )
        )

    def _hold_stop_for_budget_grace(self, decision: SolverDecision) -> SolverDecision:
        """Keep the loop alive inside the post-exhaustion grace window.

        A usage dimension that crossed its ceiling steers finalize first; the
        policy's budget STOP goes through only once the grace window has
        elapsed, which is the same edge the adapters kill on. Without the
        hold, exhaustion would stop the session before the summary is written.
        """
        if (
            decision.action is SolverAction.STOP
            and decision.reason == "budget_exhausted"
            and self.progress.budget_exceeded_at is not None
            and not self.progress.budget_grace_elapsed
        ):
            return SolverDecision(SolverAction.CONTINUE, "budget_grace_window")
        return decision

    async def _maybe_reask_finalize(self, session: AgentSession, *, event_type: str) -> None:
        """Re-ask for the summary once a settled Agent has not written it.

        A steer injected into a busy turn can be missed entirely; a settled
        Agent gets a fresh ``prompt`` instead, which is what actually produces
        the report.
        """
        if (
            not self._finalize_steered
            or self._finalize_prompted
            or self.progress.objective_completed
            or event_type != "pi.agent_settled"
        ):
            return
        if self.summary_path.is_file():
            return
        self._finalize_prompted = True
        self.trace.emit(
            "solver.finalize", actor="harness", data={"reason": "summary_missing"}
        )
        await session.observe(
            AgentObservation(
                type="solver.finalize",
                data={
                    "reason": "summary_missing",
                    "summary_file": "agent.summary.md",
                    "message": (
                        "Your round ended without a summary. This instruction overrides any "
                        "earlier instruction to keep working: run no more commands. Write "
                        "$HARNESS_SUMMARY_FILE now, based only on what you already confirmed: "
                        "confirmed facts with their evidence, unconfirmed hypotheses, what you "
                        "tried and ruled out, and unfinished intents. Then stop."
                    ),
                },
            )
        )

    def decide(
        self,
        *,
        event_type: str | None = None,
    ) -> SolverDecision:
        return decide_solver_action(
            event_type=event_type,
            no_progress_count=self.progress.no_progress_count,
            unconfirmed_progress=self.progress.unconfirmed_progress,
            remaining_budget_fraction=self.progress.remaining_budget_fraction,
            max_no_progress=self.max_no_progress,
            max_unconfirmed_progress=self.max_unconfirmed_progress,
        )

    def _absorb_operator_hint(self, hint: HintRecord, *, late: bool = False) -> bool:
        """Record an operator hint in World and trace; False when already delivered.

        The hint is advisory guidance recorded at evidence tier: the Agent and
        the end-of-run summary see it, but it never verifies the objective on
        its own. Steering stays with the caller, because a hint that arrives
        while the session is closing is only absorbed into World state.
        """
        if hint.id in self._delivered_hint_ids:
            return False
        self._delivered_hint_ids.add(hint.id)
        self.world.upsert(
            "observation",
            Observation(
                id=f"human.hint:{hint.id}",
                type="human.hint",
                content={"text": hint.text},
                confidence=1.0,
                source="harness.hint",
                provenance=Provenance(epistemic_status="evidence"),
            ),
            actor="harness",
        )
        data: dict = {"source": "operator", "hint_id": hint.id, "text": hint.text}
        if late:
            data["late"] = True
        self.trace.emit("solver.hint", actor="harness", data=data)
        return True

    def finish_ingest(self) -> None:
        report = self.world_inbox.poll()
        self.stats.inbox_accepted += report.accepted
        self.stats.inbox_rejected += report.rejected
        self.trace.emit(
            "world.ingested",
            data={
                "accepted": report.accepted,
                "rejected": report.rejected,
                "errors": [error.model_dump() for error in report.errors[:10]],
            },
        )
        # Late hints still land in the World so the end-of-run summary can
        # include guidance the operator added while the session was closing.
        for hint in self.hint_cursor.poll():
            self._absorb_operator_hint(hint, late=True)

    def _write_context(self) -> None:
        # One snapshot for focus and render keeps the written context
        # revision-consistent instead of two reloads apart.
        snapshot = self.world.snapshot
        focus = self.context_builder.build_focus(
            snapshot,
            reference=self.context_query,
            subgoal=self.progress.current_subgoal,
        )
        rendered = self.context_builder.render(
            snapshot,
            query=self.context_query,
            focus=focus,
        )
        (self.run_dir / "world.context.txt").write_text(rendered, encoding="utf-8")
        self._sync_agent_views()

    def carry_forward_brief(self, reason: str) -> str:
        """Summarise the run into the state a fresh session must inherit.

        The brief is the short version of the transcript that is about to be
        discarded: what is established, what is ruled out, what is still open
        and where the durable notes live. It is bounded so it fits any context.
        """
        snapshot = self.world.snapshot
        lines = [
            "# Carry-forward brief",
            f"reason: {reason}",
            f"world revision: {snapshot.revision}",
            f"tool calls so far: {self.progress.tool_calls}",
            "",
            "## Established facts",
        ]
        facts: list[str] = list(self.progress.confirmed_facts)
        for observation in sorted(snapshot.observations.values(), key=lambda item: item.id):
            trust = getattr(observation.provenance, "epistemic_status", "unclassified")
            if trust not in {"evidence", "verified"}:
                continue
            summary = observation.content.get("summary")
            text = str(summary) if isinstance(summary, str) and summary else observation.id
            if text not in facts:
                facts.append(text)
        lines.extend(f"- {fact}" for fact in facts[:CONTEXT_BRIEF_FACTS])
        if not facts:
            lines.append("- none recorded")

        lines.extend(["", "## Ruled out"])
        ruled_out = [
            item
            for item in sorted(snapshot.hypotheses.values(), key=lambda item: item.id)
            if item.status in {"refuted", "superseded"}
        ]
        lines.extend(
            f"- [{item.status}] {item.statement}" for item in ruled_out[:CONTEXT_BRIEF_RULED_OUT]
        )
        failures = sorted(snapshot.failures.values(), key=lambda item: item.id)
        lines.extend(
            f"- [failure] {item.message}" for item in failures[-CONTEXT_BRIEF_FAILURES:]
        )
        if not ruled_out and not failures:
            lines.append("- none recorded")

        lines.extend(["", "## Open intents"])
        intents: list[str] = []
        if self.progress.current_subgoal:
            intents.append(f"subgoal: {self.progress.current_subgoal}")
        intent = self.progress.action_intent or {}
        description = intent.get("description")
        if isinstance(description, str) and description:
            intents.append(f"last declared intent: {description}")
        for hypothesis in sorted(snapshot.hypotheses.values(), key=lambda item: item.id):
            if hypothesis.status in {"open", "supported"}:
                intents.append(f"[{hypothesis.status}] {hypothesis.statement}")
        lines.extend(f"- {item}" for item in intents[:CONTEXT_BRIEF_FACTS])
        if not intents:
            lines.append("- none recorded")

        lines.extend(
            [
                "",
                "## Pointers",
                f"- notes: {self.notes_dir if self.notes_dir is not None else 'notes directory'}",
                "- durable state: world.context.txt (Harness-rendered fact view)",
                "- write your final report to the summary file before you stop",
                "",
                (
                    "Continue from this state: do not re-derive what is listed above, and do "
                    "not re-walk a ruled-out path unless you have new evidence."
                ),
            ]
        )
        brief = "\n".join(lines) + "\n"
        if len(brief) > CONTEXT_BRIEF_MAX_CHARS:
            brief = brief[:CONTEXT_BRIEF_MAX_CHARS].rstrip() + "\n[brief truncated]\n"
        return brief

    def write_carry_forward_brief(self, reason: str) -> tuple[Path, str]:
        """Write the brief into the run directory and return it with its path."""
        brief = self.carry_forward_brief(reason)
        path = self.run_dir / "carry-forward.md"
        path.write_text(brief, encoding="utf-8")
        return path, brief

    def _sync_agent_views(self) -> None:
        if self.sync_agent_views is not None:
            self.sync_agent_views()
