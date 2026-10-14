from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .aci import TypedACI
from .action_verifier import ActionVerifier
from .cognition import frontier_context, round_brief, ruled_out_intents
from .hints import HINTS_FILE, HintCursor
from .planner import RollingHorizonPlanner, RollingPlan
from .progress import ProgressLedger
from .session import AgentEvent, AgentObservation, AgentSession
from .skills import SkillSpec
from .solver_policy import (
    DEFAULT_MAX_NO_PROGRESS,
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_UNCONFIRMED_PROGRESS,
    SolverAction,
    SolverDecision,
    decide_solver_action,
)
from .trace import TraceRecorder

MAX_PROCESSED_EVENT_IDS = 50_000
# Cap on distinct "attempted without gaining a fact" keys kept in progress.
MAX_NO_GAIN_KEYS = 50
from .verification import AuthorizedActionVerdict, VerifierRegistry
from .world import (
    Observation,
    Provenance,
    WorldContextBuilder,
    WorldRepository,
    WorldSnapshot,
    is_agent_actor,
    is_bookkeeping_record,
)
from .world.live import WorldInboxCursor


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
    # Tool results without a new established fact or ruled-out path before the
    # Harness opens a new round to break the current line of attack.
    ROUND_STALL_STEPS: ClassVar[int] = 6

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
        skills: list[SkillSpec] | None = None,
        planner: RollingHorizonPlanner | None = None,
        planner_enabled: bool = True,
        world_context_enabled: bool = True,
        plan_horizon: int = 3,
        max_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
        max_unconfirmed_progress: int = DEFAULT_MAX_UNCONFIRMED_PROGRESS,
        max_replans: int = DEFAULT_MAX_REPLANS,
        processed_event_ids: dict[str, str] | None = None,
        world_inbox_path: Path | None = None,
        hint_cursor: HintCursor | None = None,
        sync_agent_views: Callable[[], None] | None = None,
        summary_path: Path | None = None,
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
        self.sync_agent_views = sync_agent_views
        self.plan_path = run_dir / "plan.json"
        self.skills = list(skills or [])
        self.planner = planner or RollingHorizonPlanner()
        self.planner_enabled = planner_enabled
        self.world_context_enabled = world_context_enabled
        self.plan_horizon = max(1, min(3, plan_horizon))
        self.max_no_progress = max_no_progress
        self.max_unconfirmed_progress = max_unconfirmed_progress
        self.max_replans = max_replans
        self._last_plan_signature: tuple | None = None
        self._last_plan: RollingPlan | None = None
        self._budget_steers_fired: set[float] = set()
        self._finalize_steered = False
        self._finalize_prompted = False
        self._round_count = 1
        self._round_fingerprint: tuple[frozenset[str], frozenset[str]] | None = None
        self._round_brief_text: str | None = None
        self._steps_without_new_facts = 0
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
        if event.type == "tool.call":
            self._apply_plan_fallback_observation()
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
        semantic_agent_update = event.type in {
            "world.observe",
            "world.hypothesis",
            "world.capability",
            "world.artifact",
            "world.failure",
        }
        if (semantic_agent_update and aci_mutations) or live_ingest.accepted:
            self.progress.request_replan("world_state_changed")

        # Operator hints: advisory guidance recorded at evidence tier so the
        # Agent sees them, but never an objective verdict. A new hint also
        # requests a replan, mirroring how benchmark feedback re-enters planning.
        for hint in self.hint_cursor.poll():
            if hint.id in self._delivered_hint_ids:
                continue
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
            self.progress.request_replan("hint_received")
            self.trace.emit(
                "solver.hint",
                actor="harness",
                data={"hint_id": hint.id, "text": hint.text},
            )
            await session.observe(
                AgentObservation(
                    type="solver.hint",
                    data={
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
                        self.progress.request_replan("evidence_confirmed")
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
        await self._maybe_steer_budget(session)
        await self._maybe_steer_finalize(session)
        await self._maybe_reask_finalize(session, event_type=event.type)
        await self._maybe_start_round(
            session, event_type=event.type, world_changed=after_revision != before_revision
        )
        decision = self.decide(event_type=event.type)
        if decision.action is SolverAction.REPLAN:
            plan = await self.maybe_replan(session)
            if plan is None:
                decision = SolverDecision(SolverAction.CONTINUE, "replan_suppressed")
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
                "replan_count": self.progress.replan_count,
                "remaining_budget_fraction": self.progress.remaining_budget_fraction,
            },
        )

    @property
    def processed_event_ids(self) -> dict[str, str]:
        return dict(self._processed_event_ids)

    async def maybe_replan(
        self,
        session: AgentSession,
        *,
        force: bool = False,
    ) -> RollingPlan | None:
        if not self.planner_enabled:
            return None
        decision = self.decide(force_replan=force)
        if decision.action is not SolverAction.REPLAN:
            return None
        reasons = list(self.progress.replan_reasons)
        if self.progress.no_progress_count >= 2 and "no_progress_threshold" not in reasons:
            reasons.append("no_progress_threshold")
        if not force and not reasons:
            return None

        snapshot = self.world.snapshot
        plan = self.planner.propose(
            snapshot,
            self.skills,
            progress=self.progress,
            horizon=self.plan_horizon,
        )
        # Suppress replans that would recommend the same actions. The raw world
        # revision (and the heuristic score) changes on almost every event, so
        # keying on either re-planned — and re-notified the Agent — for facts
        # that do not change the recommended action set.
        signature = (
            tuple(action.skill_id for action in plan.actions),
            tuple(reasons),
            self.progress.no_progress_count,
            (self.progress.last_verification or {}).get("status"),
        )
        if not force and signature == self._last_plan_signature:
            return None

        self.plan_path.write_text(
            plan.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        self.planned_world_revision = snapshot.revision
        self.progress.replan_count += 1
        payload = plan.model_dump(mode="json")
        self.trace.emit(
            "solver.plan.updated",
            actor="harness",
            data=payload,
        )
        self.progress.write(self.progress_path)
        self._sync_agent_views()
        await session.observe(
            AgentObservation(
                type="solver.plan.updated",
                data={
                    **payload,
                    "plan_path": "plan.json",
                },
            )
        )
        self.progress.consume_replan_reasons(reasons)
        self.progress.write(self.progress_path)
        self._last_plan_signature = signature
        self._last_plan = plan
        return plan

    def _apply_plan_fallback_observation(self) -> None:
        """Fill the verify expectation from the plan when the Agent declared none.

        ``ActionVerifier.verify`` returns ``None`` when ``expected_observation`` is
        unset, which silently disables action-level verification (and its
        feedback) for every tool call. The published plan already carries the
        expected observations for each recommended skill, so use it strictly as a
        fallback: an Agent-declared expectation always wins, and this never
        produces a ``verified`` verdict on its own.
        """
        if self.progress.expected_observation or self._last_plan is None:
            return
        actions = self._last_plan.actions
        if not actions:
            return
        skill_id = (self.progress.action_intent or {}).get("skill_id")
        action = next((item for item in actions if item.skill_id == skill_id), actions[0])
        if action.expected_observations:
            self.progress.expected_observation = action.expected_observations[0]

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

    async def _maybe_steer_finalize(self, session: AgentSession) -> None:
        """Stop the Agent exploring at the soft deadline and ask for a summary.

        The run is not cut off: the wall-clock limit is soft, and the remaining
        grace window belongs to writing down what was found (facts) and what was
        being attempted (intents) so a timed-out run still leaves usable state.
        """
        if self._finalize_steered or self.progress.objective_completed:
            return
        if not self.progress.wall_time_expired:
            return
        self._finalize_steered = True
        self.trace.emit("solver.finalize", actor="harness", data={"reason": "soft_time_limit"})
        await session.observe(
            AgentObservation(
                type="solver.finalize",
                data={
                    "reason": "soft_time_limit",
                    "summary_file": "agent.summary.md",
                    "message": (
                        "Time is up, and this finalize instruction overrides any earlier "
                        "instruction that told you to keep working. Stop launching new "
                        "probes and do not run any more commands. Based only on what you "
                        "have already confirmed, write a short report to "
                        "$HARNESS_SUMMARY_FILE covering: confirmed facts (with the evidence "
                        "that confirms them), unconfirmed hypotheses, what you tried and what "
                        "it ruled out, and the intents you were pursuing but did not finish. "
                        "Then stop."
                    ),
                },
            )
        )

    def _round_state(self, snapshot: WorldSnapshot) -> tuple[frozenset[str], frozenset[str]]:
        """Established fact ids and ruled-out intent ids currently in the graph.

        Harness bookkeeping is excluded: every tool result also writes a
        ``tool.execution`` observation at evidence tier, which would otherwise
        look like a new fact on every step. Agent-authored claims count as graph
        change too - they are new knowledge the run recorded, even though the
        verdict remains open until independent evidence confirms them.
        """
        established = frozenset(
            record.id
            for store in (snapshot.observations, snapshot.capabilities, snapshot.artifacts)
            for record in store.values()
            if not is_bookkeeping_record(record)
            and (
                record.provenance.epistemic_status in {"evidence", "verified"}
                or is_agent_actor(record.provenance.actor)
            )
        )
        return established, frozenset(ruled_out_intents(snapshot, self.progress))

    def _note_attempt(self, *, gained_fact: bool) -> None:
        """Count an attempt that produced no new fact, keyed by tool+arguments."""
        if gained_fact:
            return
        action = self.progress.last_action or {}
        tool = action.get("tool")
        if not isinstance(tool, str) or not tool:
            return
        family = action.get("args_family")
        key = family if isinstance(family, str) and family else tool
        counts = self.progress.no_gain_tools
        counts[key] = counts.get(key, 0) + 1
        if len(counts) > MAX_NO_GAIN_KEYS:
            for stale in list(counts)[: len(counts) - MAX_NO_GAIN_KEYS]:
                counts.pop(stale, None)

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

    async def _maybe_start_round(
        self,
        session: AgentSession,
        *,
        event_type: str,
        world_changed: bool,
    ) -> None:
        """Open a new round once the fact/intent graph actually changed.

        A round is a fresh ``prompt`` on the same session carrying a compiled
        brief: what the previous round established, what it ruled out, and the
        open frontier. It fires on new evidence, on a newly ruled-out path, or
        after ``ROUND_STALL_STEPS`` tool results with no new fact at all.
        The graph can only change when the world revision moved or a tool
        result updated the failure ledger, so other events skip the projection.
        """
        if self._finalize_steered or self.progress.objective_completed:
            return
        if event_type != "tool.result" and not world_changed:
            return
        snapshot = self.world.snapshot
        established, ruled = self._round_state(snapshot)
        if self._round_fingerprint is None:
            # First observation of the graph is the baseline; it does not by
            # itself count as "the graph changed".
            self._round_fingerprint = (established, ruled)
        previous_facts, previous_ruled = self._round_fingerprint
        changed = bool(established - previous_facts or ruled - previous_ruled)
        if event_type == "tool.result":
            self._note_attempt(gained_fact=bool(established - previous_facts))
        if changed:
            self._steps_without_new_facts = 0
        elif event_type == "tool.result":
            self._steps_without_new_facts += 1
        stalled = (
            not changed
            and event_type == "tool.result"
            and self._steps_without_new_facts >= self.ROUND_STALL_STEPS
        )
        if not changed and not stalled:
            return

        self._round_fingerprint = (established, ruled)
        self._round_count += 1
        brief = round_brief(
            snapshot,
            self.skills,
            self.progress,
            round_number=self._round_count,
        )
        if not stalled and brief == self._round_brief_text:
            return
        self._round_brief_text = brief
        reason = "stall" if stalled else "graph_changed"
        path = self.run_dir / f"round-{self._round_count}.md"
        path.write_text(brief + "\n", encoding="utf-8")
        self._steps_without_new_facts = 0
        self.trace.emit(
            "solver.round",
            actor="harness",
            data={"round": self._round_count, "reason": reason, "path": path.name},
        )
        await session.observe(
            AgentObservation(
                type="solver.round",
                data={
                    "round": self._round_count,
                    "reason": reason,
                    "path": path.name,
                    "brief": brief,
                },
            )
        )

    def decide(
        self,
        *,
        event_type: str | None = None,
        force_replan: bool = False,
    ) -> SolverDecision:
        return decide_solver_action(
            event_type=event_type,
            replan_reasons=self.progress.replan_reasons,
            no_progress_count=self.progress.no_progress_count,
            unconfirmed_progress=self.progress.unconfirmed_progress,
            replans_used=self.progress.replan_count,
            remaining_budget_fraction=self.progress.remaining_budget_fraction,
            force_replan=force_replan,
            max_no_progress=self.max_no_progress,
            max_unconfirmed_progress=self.max_unconfirmed_progress,
            max_replans=self.max_replans,
        )

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
            if hint.id in self._delivered_hint_ids:
                continue
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
            self.trace.emit(
                "solver.hint",
                actor="harness",
                data={"hint_id": hint.id, "late": True},
            )

    def _write_context(self) -> None:
        if self.world_context_enabled:
            # One snapshot for focus, render and frontier keeps the written
            # context revision-consistent instead of three reloads apart.
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
            rendered += "\n" + frontier_context(snapshot, self.skills)
        else:
            rendered = ""
        (self.run_dir / "world.context.txt").write_text(rendered, encoding="utf-8")
        self._sync_agent_views()

    def _sync_agent_views(self) -> None:
        if self.sync_agent_views is not None:
            self.sync_agent_views()
