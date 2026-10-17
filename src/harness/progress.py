from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .budget import USAGE_DIMENSION_SOURCES, exceeded_usage_dimension
from .session import AgentEvent
from .verification import AuthorizedObjectiveVerdict, VerifierRegistry

# Consecutive calls in one command family before the Harness intervenes, and
# the streak at which it stops nudging and asks for convergence instead.
REPETITION_STEER_STREAK = 3
REPETITION_ESCALATE_STREAK = 6
REPETITION_FAMILY_FAILURES = REPETITION_STEER_STREAK
REPETITION_FINGERPRINT_STREAK = REPETITION_STEER_STREAK
# Repetition bookkeeping is bounded so a long run cannot grow it without limit.
MAX_TRACKED_TOOL_FAMILIES = 200

# A long single session degrades: the transcript grows, noise crowds out the
# signal, and the Agent starts re-deriving what it already knew. These are the
# defaults at which the Harness resets the session and re-injects a brief. The
# tool-call triggers are counted from the previous restart, so each reset needs
# a fresh stretch of work before the next one is considered.
CONTEXT_RESTART_TOOL_CALLS = 300
CONTEXT_RESTART_BUDGET_FRACTION = 0.6
MAX_CONTEXT_RESTARTS = 2

# Which delivered Agent observation feeds which metric bucket. The names are
# the ones the 2026-10-16 sweep review used by hand: "world" and "verification"
# were 93% of the traffic and carried no action-able information, which is why
# the improvement plan quiets them instead of adding more of them.
STEER_BUCKETS: dict[str, str] = {
    "world.state.updated": "world",
    "solver.verification": "verification",
    "solver.repetition_steer": "repetition",
    "solver.budget": "budget",
    "solver.finalize": "finalize",
    "solver.submission_due": "submission",
    "solver.submission_reask": "submission",
    "solver.closing_session": "submission",
}
STEER_BUCKET_NAMES: tuple[str, ...] = (
    "world",
    "verification",
    "repetition",
    "budget",
    "finalize",
    "submission",
)
# Slow-call telemetry is a bounded top-N list, not a per-call log: the run
# result must stay small enough to aggregate a whole sweep.
SLOW_TOOL_CALLS = 5


def empty_steer_counts() -> dict[str, int]:
    return dict.fromkeys(STEER_BUCKET_NAMES, 0)


def _format_seconds(seconds: float) -> str:
    """Compact duration for the Agent-facing budget line (e.g. ``11m12s``)."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class ProgressHypothesis(BaseModel):
    id: str
    statement: str | None = None
    status: str = "open"
    confidence: float | None = None
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)


class ProgressLedger(BaseModel):
    schema_version: str = "harness/progress/v4"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_goal: str | None = None
    completed_subgoals: list[str] = Field(default_factory=list)
    claimed_completed_subgoals: list[str] = Field(default_factory=list)
    completion_claims: list[str] = Field(default_factory=list)
    blocked_subgoals: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    # Kept for readers of v1 progress files. Agent-authored facts are no
    # longer added here; only independently verified facts belong here.
    confirmed_facts: list[str] = Field(default_factory=list)
    current_subgoal: str | None = None
    hypotheses: dict[str, ProgressHypothesis] = Field(default_factory=dict)
    action_intent: dict[str, Any] | None = None
    expected_observation: str | None = None
    actual_observation: str | None = None
    last_verification: dict[str, Any] | None = None
    verified_actions: int = 0
    execution_succeeded: int = 0
    evidence_confirmed: int = 0
    capability_acquired: int = 0
    goal_advanced: int = 0
    contradicted_actions: int = 0
    pending_actions: int = 0
    # Verdicts the Harness could not decide because it had no independent
    # observation: recorded, never steered, never mistaken for a stall.
    unavailable_actions: int = 0
    skipped_verifications: int = 0
    last_action: dict[str, Any] | None = None
    active_actions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    failure_count: int = 0
    no_progress_count: int = 0
    # Consecutive tool executions without *confirmed* progress. A successful
    # execution is neutral (neither confirmed progress nor a stall), so it feeds
    # this bounded counter instead of the hard no-progress stall counter. This
    # keeps exploration alive when no action-level verifier is available while
    # still terminating an endless unproductive loop.
    unconfirmed_progress: int = 0
    failed_skill_counts: dict[str, int] = Field(default_factory=dict)
    reconciled_actions: dict[str, str] = Field(default_factory=dict)
    # Repetition control: how many tool calls in a row share one command family,
    # which families already drew an intervention, and the per-family totals.
    # A family that keeps repeating without confirmed progress is the signature
    # of a loop being ground down rather than a line of attack being widened.
    tool_family_streak: int = 0
    last_tool_family: str | None = None
    tool_family_totals: dict[str, int] = Field(default_factory=dict)
    steered_tool_families: dict[str, int] = Field(default_factory=dict)
    # Repetition is a signal only when a family keeps failing or the exact
    # same arguments repeat. Distinct commands in one broad family (many
    # different curl requests, for example) are exploration, not grinding.
    tool_family_failures: dict[str, int] = Field(default_factory=dict)
    last_failed_tool_family: str | None = None
    tool_fingerprint_streak: int = 0
    last_tool_fingerprint: str | None = None
    # Session restarts performed after the context grew long: the counter and
    # the tool-call mark let the next threshold be measured from the restart
    # instead of the run start.
    context_restarts: int = 0
    tool_calls_at_last_restart: int = 0
    # Correlates legacy events that predate tool_call_id, so replay uses the
    # same generated action identity after a checkpoint reload.
    tool_event_correlations: dict[str, str] = Field(default_factory=dict)
    budget_limits: dict[str, float] = Field(default_factory=dict)
    budget_used: dict[str, float] = Field(default_factory=dict)
    # Extra wall-clock room granted after the soft deadline so a run can stop
    # exploring and write its final summary instead of being cut off.
    wall_time_grace_seconds: float = Field(default=0.0, ge=0.0)
    # When a usage dimension first crossed its ceiling. The solver holds the
    # budget STOP inside the grace window counted from this stamp, mirroring
    # the adapters' kill deadline, so exhaustion cannot cut the summary short.
    budget_exceeded_at: datetime | None = None
    tool_calls: int = 0
    model_calls: int = 0
    accepted_submissions: int = 0
    rejected_submissions: int = 0
    # Every candidate handed to the evaluator, accepted or rejected. The
    # accepted/rejected split cannot answer "did this run ever try?".
    submissions_attempted: int = 0
    objective_completed: bool = False
    objective_verdict: dict[str, Any] | None = None
    last_event_type: str | None = None
    last_event_id: str | None = None
    event_count: int = 0

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        return value

    def record_event(self, event: AgentEvent) -> None:
        self.event_count += 1
        self.last_event_type = event.type
        self.last_event_id = event.event_id
        if event.type == "tool.call":
            self.tool_calls += 1
            self.actual_observation = None
            action = {
                "tool": event.data.get("tool"),
                "tool_call_id": event.data.get("tool_call_id"),
                "status": "running",
                "skill_id": (self.action_intent or {}).get("skill_id"),
            }
            family = event.data.get("args_family")
            if isinstance(family, str) and family:
                action["args_family"] = family
            fingerprint = event.data.get("args_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                action["args_fingerprint"] = fingerprint
            self._record_tool_family(family)
            self.last_action = action
            call_id = action["tool_call_id"]
            if isinstance(call_id, str) and call_id:
                self.active_actions[call_id] = dict(action)
            expected = event.data.get("expected_observation")
            if isinstance(expected, str) and expected:
                self.expected_observation = expected
        elif event.type == "tool.result":
            exit_code = event.data.get("exit_code")
            failed = bool(event.data.get("is_error", False)) or (
                isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and exit_code != 0
            )
            call_id = event.data.get("tool_call_id")
            active_action = self.active_actions.pop(call_id, {}) if isinstance(call_id, str) else {}
            action = {
                "tool": event.data.get("tool"),
                "tool_call_id": call_id,
                "status": "failed" if failed else "succeeded",
                "skill_id": active_action.get("skill_id")
                or (self.action_intent or {}).get("skill_id"),
            }
            family = active_action.get("args_family")
            if isinstance(family, str) and family:
                action["args_family"] = family
            fingerprint = active_action.get("args_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                action["args_fingerprint"] = fingerprint
            self.last_action = action
            actual = event.data.get("observation")
            if isinstance(actual, str) and actual:
                self.actual_observation = actual
            if failed:
                self.failure_count += 1
                skill_id = action.get("skill_id")
                if isinstance(skill_id, str) and skill_id:
                    self.failed_skill_counts[skill_id] = (
                        self.failed_skill_counts.get(skill_id, 0) + 1
                    )
            else:
                self.execution_succeeded += 1
            # A process exit is execution evidence only. A failed run is a real
            # stall signal; a successful run is neither confirmed progress nor a
            # stall, so it only advances the bounded unconfirmed counter.
            if failed:
                self.no_progress_count += 1
            self.unconfirmed_progress += 1
            self._record_repetition_outcome(
                family,
                fingerprint,
                failed=failed,
            )
        elif event.type == "progress.updated":
            self._record_progress_update(event.data)
        elif event.type == "goal.completed":
            claim = event.data.get("goal_id") or self.active_goal or "objective"
            if isinstance(claim, str) and claim not in self.completion_claims:
                self.completion_claims.append(claim)
        elif event.type == "model.request":
            count = event.data.get("count", 1)
            if isinstance(count, int) and count > 0:
                self.model_calls += count
        elif event.type == "model.usage":
            usage = event.data
            for field, source in (
                ("tokens", "total_tokens"),
                ("cost_usd", "cost_usd"),
            ):
                value = usage.get(source)
                if isinstance(value, (int, float)) and value >= 0:
                    self.budget_used[field] = self.budget_used.get(field, 0.0) + float(value)

    def _record_tool_family(self, family: str | None) -> None:
        """Track consecutive tool calls that share one command family."""
        if not isinstance(family, str) or not family:
            self.tool_family_streak = 0
            self.last_tool_family = None
            return
        if family == self.last_tool_family:
            self.tool_family_streak += 1
        else:
            self.last_tool_family = family
            self.tool_family_streak = 1
        self.tool_family_totals[family] = self.tool_family_totals.get(family, 0) + 1
        while len(self.tool_family_totals) > MAX_TRACKED_TOOL_FAMILIES:
            self.tool_family_totals.pop(next(iter(self.tool_family_totals)))

    def _record_repetition_outcome(
        self,
        family: str | None,
        fingerprint: str | None,
        *,
        failed: bool,
    ) -> None:
        if isinstance(fingerprint, str) and fingerprint:
            if fingerprint == self.last_tool_fingerprint:
                self.tool_fingerprint_streak += 1
            else:
                self.last_tool_fingerprint = fingerprint
                self.tool_fingerprint_streak = 1
        else:
            self.last_tool_fingerprint = None
            self.tool_fingerprint_streak = 0

        if failed and isinstance(family, str) and family:
            self.tool_family_failures[family] = (
                self.tool_family_failures.get(family, 0) + 1
            )
            self.last_failed_tool_family = family
            while len(self.tool_family_failures) > MAX_TRACKED_TOOL_FAMILIES:
                self.tool_family_failures.pop(next(iter(self.tool_family_failures)))
        elif not failed:
            self.last_failed_tool_family = None

    def repetition_action(self) -> tuple[str, int, int, str, str | None] | None:
        """Return the pending intervention for the current family, if any.

        The tuple is ``(identity, streak, level, basis, fingerprint)`` where
        level 1 is a targeted nudge and level 2 asks the Agent to converge and
        write its summary. The basis distinguishes accumulated family failures
        from an exact argument repeat. Each level fires at most once per
        identity per run.
        """
        family = self.last_failed_tool_family or self.last_tool_family
        identity = family
        fingerprint = self.last_tool_fingerprint
        if identity is None and fingerprint:
            identity = f"fingerprint:{fingerprint[:8]}"
        if identity is None:
            return None
        emitted = self.steered_tool_families.get(identity, 0)
        if (
            fingerprint
            and self.tool_fingerprint_streak >= REPETITION_ESCALATE_STREAK
            and emitted < 2
        ):
            return identity, self.tool_fingerprint_streak, 2, "identical_args", fingerprint
        if (
            fingerprint
            and self.tool_fingerprint_streak >= REPETITION_FINGERPRINT_STREAK
            and emitted < 1
        ):
            return identity, self.tool_fingerprint_streak, 1, "identical_args", fingerprint

        failures = self.tool_family_failures.get(family or "", 0)
        if failures >= REPETITION_ESCALATE_STREAK and emitted < 2:
            return identity, failures, 2, "failure_family", fingerprint
        if failures >= REPETITION_FAMILY_FAILURES and emitted < 1:
            return identity, failures, 1, "failure_family", fingerprint
        return None

    def note_repetition_steer(self, family: str, *, level: int) -> None:
        self.steered_tool_families[family] = max(
            self.steered_tool_families.get(family, 0), level
        )
        while len(self.steered_tool_families) > MAX_TRACKED_TOOL_FAMILIES:
            self.steered_tool_families.pop(next(iter(self.steered_tool_families)))

    def reset_repetition(self) -> None:
        """Clear the streak after confirmed progress; steered families stay."""
        self.tool_family_streak = 0
        self.last_tool_family = None
        self.tool_family_totals.clear()
        self.tool_family_failures.clear()
        self.last_failed_tool_family = None
        self.tool_fingerprint_streak = 0
        self.last_tool_fingerprint = None

    def context_restart_due(self) -> str | None:
        """Return why the session context should be reset, or ``None``.

        Both triggers are counted from the previous restart, so a restart
        cannot re-fire on the threshold it just crossed. Only while the
        objective is still open: a finished objective has nothing left to
        re-inject.
        """
        if self.objective_completed or self.context_restarts >= MAX_CONTEXT_RESTARTS:
            return None
        calls_since_restart = self.tool_calls - self.tool_calls_at_last_restart
        if calls_since_restart >= CONTEXT_RESTART_TOOL_CALLS:
            return "tool_calls"
        limit = self.budget_limits.get("max_tool_calls", 0.0)
        if limit > 0 and calls_since_restart >= limit * CONTEXT_RESTART_BUDGET_FRACTION:
            return "tool_call_budget"
        return None

    def record_context_restart(self) -> None:
        self.context_restarts += 1
        self.tool_calls_at_last_restart = self.tool_calls
        self.reset_repetition()

    def record_intent(self, intent: Any) -> None:
        data = intent.model_dump(mode="json") if hasattr(intent, "model_dump") else dict(intent)
        self.action_intent = data
        skill_id = data.get("skill_id")
        if isinstance(skill_id, str) and skill_id:
            self.action_intent["skill_id"] = skill_id
        subgoal = data.get("subgoal_id")
        if isinstance(subgoal, str) and subgoal:
            self.current_subgoal = subgoal
        expected = data.get("expected_observations") or []
        self.expected_observation = (
            str(expected[0]) if isinstance(expected, list) and expected else None
        )
        self.actual_observation = None

    def _record_progress_update(self, data: dict[str, Any]) -> None:
        current = data.get("current_subgoal", data.get("subgoal"))
        if isinstance(current, str) and current:
            self.current_subgoal = current

        completed = data.get("completed_subgoal")
        if isinstance(completed, str) and completed not in self.claimed_completed_subgoals:
            self.claimed_completed_subgoals.append(completed)

        if data.get("objective_completed") is True:
            claim = str(data.get("goal_id") or self.active_goal or "objective")
            if claim not in self.completion_claims:
                self.completion_claims.append(claim)

        blocked = data.get("blocked_subgoal")
        if isinstance(blocked, str) and blocked not in self.blocked_subgoals:
            self.blocked_subgoals.append(blocked)

        fact = data.get("confirmed_fact")
        if isinstance(fact, str) and fact not in self.claims:
            self.claims.append(fact)

        expected = data.get("expected_observation")
        if isinstance(expected, str) and expected:
            self.expected_observation = expected

        actual = data.get("actual_observation")
        if isinstance(actual, str) and actual:
            self.actual_observation = actual

        hypothesis = data.get("hypothesis")
        if isinstance(hypothesis, dict) and isinstance(hypothesis.get("id"), str):
            item = ProgressHypothesis.model_validate(hypothesis)
            self.hypotheses[item.id] = item

        made_progress = data.get("made_progress")
        if made_progress is False:
            self.no_progress_count += 1
            self.unconfirmed_progress += 1

    def record_verification(self, verification: Any) -> None:
        data = (
            verification.model_dump(mode="json")
            if hasattr(verification, "model_dump")
            else dict(verification)
        )
        self.last_verification = data
        status = data.get("status")
        if status == "verified":
            self.verified_actions += 1
            self.evidence_confirmed += 1
            self.no_progress_count = 0
            self.unconfirmed_progress = 0
            self.reset_repetition()
        elif status == "contradicted":
            self.contradicted_actions += 1
            if (self.last_action or {}).get("status") != "failed":
                self.failure_count += 1
                self.no_progress_count += 1
            self.unconfirmed_progress += 1
        elif status == "pending":
            self.pending_actions += 1
        elif status == "unavailable":
            self.unavailable_actions += 1

    def record_submission(self, *, accepted: bool, completed: bool) -> None:
        del completed  # Final evaluation, not submission feedback, owns completion.
        self.submissions_attempted += 1
        if accepted:
            self.accepted_submissions += 1
            self.no_progress_count = 0
            self.unconfirmed_progress = 0
            self.reset_repetition()
        else:
            self.rejected_submissions += 1
            self.failure_count += 1
            self.no_progress_count += 1
            self.unconfirmed_progress += 1
        # Submission feedback may ask the runtime to stop early, but only the
        # final evaluator can establish objective completion.

    def record_trusted_progress(self, kind: str) -> None:
        """Record progress only from a Harness-owned verifier or benchmark."""
        if kind == "evidence_confirmed":
            self.evidence_confirmed += 1
        elif kind == "capability_acquired":
            self.capability_acquired += 1
        elif kind == "goal_advanced":
            self.goal_advanced += 1
        elif kind == "objective_completed":
            raise ValueError("objective completion requires an authorized ObjectiveVerdict")
        else:
            raise ValueError(f"unknown trusted progress kind: {kind}")
        self.no_progress_count = 0
        self.unconfirmed_progress = 0
        self.reset_repetition()

    def record_objective_verdict(
        self,
        authorized: AuthorizedObjectiveVerdict,
        registry: VerifierRegistry,
    ) -> None:
        if not registry.is_authorized_objective(authorized):
            raise ValueError("objective verdict was not authorized by the run verifier registry")
        verdict = authorized.verdict
        self.objective_verdict = verdict.model_dump(mode="json")
        self.objective_completed = verdict.status == "verified"
        if self.objective_completed:
            self.no_progress_count = 0
            self.unconfirmed_progress = 0
            self.reset_repetition()

    @property
    def remaining_budget_fraction(self) -> float:
        return min(self.budget_fractions().values(), default=1.0)

    @property
    def wall_time_expired(self) -> bool:
        """True once the declared (soft) wall-clock window has elapsed."""
        limit = self.budget_limits.get("wall_time", 0.0)
        if limit <= 0:
            return False
        elapsed = max(0.0, (datetime.now(UTC) - self.started_at).total_seconds())
        return elapsed >= limit

    @property
    def usage_snapshot(self) -> dict[str, float]:
        """Charged usage keyed the way the shared dimension table reads it.

        ``model.usage`` charges tokens and cost into ``budget_used``; model and
        tool calls are counted on their own. A dimension stays absent until it
        is first charged, so a caller can tell "nothing charged yet" from zero.
        """
        charged: dict[str, float | None] = {
            "tokens": self.budget_used.get("tokens"),
            "cost_usd": self.budget_used.get("cost_usd"),
            "model_calls": float(self.model_calls),
            "tool_calls": float(self.tool_calls),
        }
        return {key: value for key, value in charged.items() if value is not None}

    def _budget_used(self, dimension: str) -> float | None:
        """Charged usage for one enforced dimension, or ``None`` if never charged."""
        source = USAGE_DIMENSION_SOURCES.get(dimension)
        return None if source is None else self.usage_snapshot.get(source[1])

    @property
    def budget_exceeded_dimension(self) -> str | None:
        """The enforced dimension that crossed its ceiling, if any.

        Scanned by the shared budget accounting so the ledger names the same
        dimension the live monitor does when two ceilings cross at once.
        """
        if self.wall_time_expired:
            return "wall_time"
        return exceeded_usage_dimension(self.usage_snapshot, self.budget_limits)

    @property
    def budget_grace_elapsed(self) -> bool:
        """True once the post-exhaustion grace window is over."""
        if self.budget_exceeded_at is None:
            return False
        elapsed = (datetime.now(UTC) - self.budget_exceeded_at).total_seconds()
        return elapsed >= self.wall_time_grace_seconds

    def agent_budget_lines(self) -> list[str]:
        """Deterministic, Agent-facing statement of what is left.

        The 2026-10-16 sweep sent 39 budget steers, all of them only once the
        budget was nearly gone, and `world.context.txt` carried no budget
        section at all; the Agent had no way to answer "how much time is left"
        except by burning a turn on it.
        """
        if not self.budget_limits:
            return ["no declared budget; the operator owns the wall clock"]
        lines: list[str] = []
        for dimension, fraction in sorted(
            self.budget_fractions().items(),
            key=lambda item: (item[0] != "wall_time", item[0]),
        ):
            if dimension == "wall_time":
                limit = self.budget_limits.get("wall_time", 0.0)
                effective = limit + self.wall_time_grace_seconds
                elapsed = max(0.0, (datetime.now(UTC) - self.started_at).total_seconds())
                lines.append(
                    f"wall_time {fraction:.0%} left "
                    f"({_format_seconds(max(0.0, effective - elapsed))} of "
                    f"{_format_seconds(effective)})"
                )
            else:
                lines.append(f"{dimension} {fraction:.0%} left")
        return lines

    def budget_fractions(self) -> dict[str, float]:
        """Per-dimension remaining budget fraction (``1.0`` when untouched)."""
        fractions: dict[str, float] = {}
        for key, limit in self.budget_limits.items():
            if limit <= 0:
                continue
            if key == "wall_time":
                elapsed = max(0.0, (datetime.now(UTC) - self.started_at).total_seconds())
                effective = limit + self.wall_time_grace_seconds
                fractions[key] = max(0.0, min(1.0, (effective - elapsed) / effective))
                continue
            used = self._budget_used(key)
            if used is None:
                continue
            fractions[key] = max(0.0, min(1.0, (limit - used) / limit))
        return fractions

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
            if hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
