from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .budget import USAGE_DIMENSION_SOURCES, exceeded_usage_dimension
from .session import AgentEvent
from .verification import AuthorizedObjectiveVerdict, VerifierRegistry

# A long single session degrades: the transcript grows, noise crowds out the
# signal, and the Agent starts re-deriving what it already knew. These are the
# defaults at which the Harness resets the session and re-injects a brief. The
# tool-call triggers are counted from the previous restart, so each reset needs
# a fresh stretch of work before the next one is considered.
CONTEXT_RESTART_TOOL_CALLS = 300
CONTEXT_RESTART_BUDGET_FRACTION = 0.6
MAX_CONTEXT_RESTARTS = 2

STEER_BUCKETS: dict[str, str] = {
    "solver.hint": "hint",
    "solver.finalize": "finalize",
    "solver.submission_due": "submission",
    "solver.submission_reask": "submission",
    "solver.closing_session": "submission",
    "benchmark.feedback": "verification",
}
STEER_BUCKET_NAMES: tuple[str, ...] = (
    "hint",
    "finalize",
    "submission",
    "verification",
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


class ProgressLedger(BaseModel):
    schema_version: str = "harness/progress/v5"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_goal: str | None = None
    last_action: dict[str, Any] | None = None
    active_actions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reconciled_actions: dict[str, str] = Field(default_factory=dict)
    context_restarts: int = 0
    tool_calls_at_last_restart: int = 0
    tool_event_correlations: dict[str, str] = Field(default_factory=dict)
    budget_limits: dict[str, float] = Field(default_factory=dict)
    budget_used: dict[str, float] = Field(default_factory=dict)
    wall_time_grace_seconds: float = Field(default=0.0, ge=0.0)
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

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_progress(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        version = value.get("schema_version")
        if isinstance(version, str) and version.startswith("harness/progress/v"):
            return {**value, "schema_version": "harness/progress/v5"}
        return value

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
            action = {
                "tool": event.data.get("tool"),
                "tool_call_id": event.data.get("tool_call_id"),
                "status": "running",
            }
            self.last_action = action
            call_id = action["tool_call_id"]
            if isinstance(call_id, str) and call_id:
                self.active_actions[call_id] = dict(action)
        elif event.type == "tool.result":
            exit_code = event.data.get("exit_code")
            failed = bool(event.data.get("is_error", False)) or (
                isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and exit_code != 0
            )
            call_id = event.data.get("tool_call_id")
            if isinstance(call_id, str):
                self.active_actions.pop(call_id, None)
            action = {
                "tool": event.data.get("tool"),
                "tool_call_id": call_id,
                "status": "failed" if failed else "succeeded",
            }
            self.last_action = action
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

    def record_submission(self, *, accepted: bool, completed: bool) -> None:
        del completed  # Final evaluation, not submission feedback, owns completion.
        self.submissions_attempted += 1
        if accepted:
            self.accepted_submissions += 1
        else:
            self.rejected_submissions += 1
        # Submission feedback may ask the runtime to stop early, but only the
        # final evaluator can establish objective completion.

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
