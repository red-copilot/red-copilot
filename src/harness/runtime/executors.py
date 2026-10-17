"""Competing solver sessions over one provisioned benchmark case (PLN-04).

One case keeps one provision, one authoritative world, one trace, one
submission dedupe set and one final evaluation. Every declared executor becomes
a separate Agent session with its own container, workspace, notes, progress
ledger and checkpoint under ``run_dir/executors/<nn>-<id>/``, so N sessions can
race without overwriting each other's control state.

Exactly one path is shared read-write with the sessions:
``run_dir/executor-shared`` mounted at ``/run/harness-shared`` holds the
agent-authored ``shared-findings.md`` so competing sessions can read what the
others confirmed. Nothing authoritative ever lives there; the Harness archives
content-changing snapshots for audit.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..agent_workspace import create_agent_workspace
from ..models import MAX_EXECUTOR_SESSIONS, AgentSpec, BudgetSpec
from ..progress import ProgressLedger
from ..secureio import read_regular_text

EXECUTOR_DIRNAME = "executors"
SHARED_DIRNAME = "executor-shared"
SHARED_FINDINGS_NAME = "shared-findings.md"
ARCHIVE_DIRNAME = "archive"

MAX_FINDINGS_BYTES = 256 * 1024
MAX_FINDINGS_ARCHIVES = 32

# Usage dimensions divided across competing sessions. Wall time is deliberately
# not split: the sessions run concurrently inside a single competition window.
SPLIT_BUDGET_FIELDS = ("max_tokens", "max_model_calls", "max_tool_calls", "max_cost_usd")

# Session counters folded into the authoritative run ledger at the end of the
# run. Every other field stays owned by the run ledger, so the shared submission
# dedupe and the objective verdict are never counted twice.
SESSION_COUNTER_FIELDS = (
    "tool_calls",
    "model_calls",
    "event_count",
    "verified_actions",
    "execution_succeeded",
    "evidence_confirmed",
    "capability_acquired",
    "goal_advanced",
    "contradicted_actions",
    "pending_actions",
    "skipped_verifications",
    "failure_count",
    "accepted_submissions",
    "rejected_submissions",
)

# Counters that measure a *consecutive* streak inside one session rather than a
# total. Adding them across sessions would report a stall no session ever ran
# into, so the run ledger keeps the worst streak instead of the sum.
SESSION_STREAK_FIELDS = ("no_progress_count", "unconfirmed_progress")

SHARED_FINDINGS_HEADER = (
    "# Shared findings\n"
    "\n"
    "Append findings other competing sessions should see. The Harness archives\n"
    "snapshots of this file. Never write credentials or raw target data here.\n"
)


@dataclass(frozen=True)
class ExecutorSessionPlan:
    """Everything one competing session needs to stay isolated."""

    index: int
    executor_id: str
    session_id: str
    agent: AgentSpec
    budgets: BudgetSpec
    root: Path
    workspace: Path


@dataclass(frozen=True)
class ExecutorPlan:
    """Sessions for one case plus the optional shared findings channel."""

    sessions: tuple[ExecutorSessionPlan, ...]
    shared_dir: Path | None = None
    findings_path: Path | None = None

    @property
    def competing(self) -> bool:
        return len(self.sessions) > 1


def split_budgets(budgets: BudgetSpec, sessions: int) -> BudgetSpec:
    """Return even shares of the usage dimensions; wall time stays whole.

    Shares are floored with a floor of one unit so no session is scheduled with
    a zero ceiling; the shares add up to the declared run budget.
    """
    if sessions < 1:
        raise ValueError("sessions must be at least 1")
    if sessions == 1:
        return budgets
    updates: dict[str, float] = {}
    for field in SPLIT_BUDGET_FIELDS:
        value = getattr(budgets, field)
        if value is None:
            continue
        if field == "max_cost_usd":
            updates[field] = max(float(value) / sessions, 1e-6)
        else:
            updates[field] = float(max(int(value) // sessions, 1))
    return budgets.model_copy(update=updates)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return (cleaned or "executor")[:64]


def plan_executor_sessions(
    *,
    agent: AgentSpec,
    budgets: BudgetSpec,
    run_dir: Path,
) -> ExecutorPlan:
    """Derive one isolated session per declared executor.

    Without declared executors the plan reproduces the historical single-session
    layout exactly: the run directory is the session root and the Agent
    workspace stays at ``run_dir/agent-workspace``.
    """
    specs = agent.with_executors()
    if len(specs) > MAX_EXECUTOR_SESSIONS:
        raise ValueError(
            f"at most {MAX_EXECUTOR_SESSIONS} competing executors are supported per case"
        )
    if len(specs) == 1:
        return ExecutorPlan(
            sessions=(
                ExecutorSessionPlan(
                    index=0,
                    executor_id=specs[0].id,
                    session_id=specs[0].id,
                    agent=specs[0],
                    budgets=budgets,
                    root=run_dir,
                    workspace=run_dir / "agent-workspace",
                ),
            )
        )
    share = split_budgets(budgets, len(specs))
    sessions: list[ExecutorSessionPlan] = []
    for index, spec in enumerate(specs):
        executor_id = agent.executors[index].id
        root = run_dir / EXECUTOR_DIRNAME / f"{index + 1:02d}-{_slug(executor_id)}"
        sessions.append(
            ExecutorSessionPlan(
                index=index,
                executor_id=executor_id,
                session_id=spec.id,
                agent=spec,
                budgets=share,
                root=root,
                workspace=root / "agent-workspace",
            )
        )
    shared_dir = run_dir / SHARED_DIRNAME
    return ExecutorPlan(
        sessions=tuple(sessions),
        shared_dir=shared_dir,
        findings_path=shared_dir / SHARED_FINDINGS_NAME,
    )


def prepare_shared_findings(plan: ExecutorPlan) -> Path | None:
    """Create the shared findings file and return its host path.

    Only competing sessions get the channel; a single session keeps working from
    its own notes and the run artifacts.
    """
    if plan.findings_path is None or plan.shared_dir is None:
        return None
    plan.shared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not plan.findings_path.exists():
        plan.findings_path.write_text(SHARED_FINDINGS_HEADER, encoding="utf-8")
    return plan.findings_path


def prepare_executor_directories(plan: ExecutorPlan) -> None:
    """Create every session root and its Agent workspace before provisioning."""
    for session in plan.sessions:
        session.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        create_agent_workspace(session.root)


class FindingsArchive:
    """Archive each distinct snapshot of the shared findings file."""

    def __init__(
        self,
        *,
        run_dir: Path,
        limit: int = MAX_FINDINGS_ARCHIVES,
    ) -> None:
        self.findings = run_dir / SHARED_DIRNAME / SHARED_FINDINGS_NAME
        self.root = run_dir / SHARED_DIRNAME / ARCHIVE_DIRNAME
        self.limit = max(limit, 1)
        self.sequence = 0
        self.digest: str | None = None

    def snapshot(self) -> Path | None:
        """Write a snapshot when the file changed since the last one."""
        if not self.findings.is_file():
            return None
        try:
            payload = read_regular_text(
                self.findings, max_bytes=MAX_FINDINGS_BYTES
            ).encode("utf-8")
        except (OSError, ValueError):
            return None
        digest = hashlib.sha256(payload).hexdigest()
        if digest == self.digest:
            return None
        self.digest = digest
        self.sequence += 1
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / f"{self.sequence:03d}-{digest[:8]}.md"
        target.write_bytes(payload)
        self._prune()
        return target

    def _prune(self) -> None:
        entries = sorted(path for path in self.root.glob("*.md") if path.is_file())
        for path in entries[: max(0, len(entries) - self.limit)]:
            path.unlink(missing_ok=True)


def fold_session_progress(
    run_progress: ProgressLedger,
    sessions: Iterable[ProgressLedger],
) -> None:
    """Fold competing session counters into the authoritative run ledger.

    Call once, after every session has stopped: the run ledger owns submissions
    and the objective verdict itself, so totals fold additively while the stall
    counters keep the longest streak any single session reached.
    """
    for ledger in sessions:
        for field in SESSION_COUNTER_FIELDS:
            setattr(run_progress, field, getattr(run_progress, field) + getattr(ledger, field))
        for field in SESSION_STREAK_FIELDS:
            setattr(
                run_progress,
                field,
                max(getattr(run_progress, field), getattr(ledger, field)),
            )
        for key, value in ledger.budget_used.items():
            run_progress.budget_used[key] = run_progress.budget_used.get(key, 0.0) + float(value)
        for skill, count in ledger.failed_skill_counts.items():
            run_progress.failed_skill_counts[skill] = (
                run_progress.failed_skill_counts.get(skill, 0) + count
            )
