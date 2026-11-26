"""Shared world and network result projections for execution entry points."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ..models import AgentSpec
from ..progress import ProgressLedger
from ..solver_loop import SolverLoop
from ..world import (
    Goal,
    WorldContextBuilder,
    WorldRepository,
    agent_actor,
    is_bookkeeping_record,
)

# Bounds so a summary of a long run stays a readable artifact.
MAX_SUMMARY_ITEMS = 40
MAX_SUMMARY_TEXT = 200
MAX_AGENT_REPORT_CHARS = 4000
# Host-side read bound for the Agent-authored report before truncation to
# MAX_AGENT_REPORT_CHARS: bounded memory on a workspace file the Agent can
# inflate, without turning an over-long report into a missing one.
MAX_AGENT_REPORT_READ_BYTES = 256 * 1024


def finalize_world_goal(
    *,
    world: WorldRepository,
    goal: Goal,
    context_builder: WorldContextBuilder,
    run_dir: Path,
    status: str,
    success: bool,
    score: float,
) -> None:
    """Persist final objective state and render the final snapshot."""
    goal.status = "completed" if success else "failed"
    goal.attributes.update({"run_status": status, "score": score, "success": success})
    world.upsert("goal", goal)
    focus = context_builder.build_focus(world.snapshot, reference=goal.description)
    (run_dir / "world.context.txt").write_text(
        context_builder.render(world.snapshot, focus=focus),
        encoding="utf-8",
    )


def world_result(
    *,
    world: WorldRepository,
    solver_loop: SolverLoop,
    agent_id: str,
    agent_ids: Iterable[str] | None = None,
    extra_solver_loops: Iterable[SolverLoop] = (),
) -> dict:
    actors = {agent_actor(item) for item in (agent_ids or (agent_id,))}
    loops = (solver_loop, *extra_solver_loops)
    return {
        "revision": world.snapshot.revision,
        "agent_authored_records": sum(
            1 for event in world.events() if event.actor in actors
        ),
        "aci": {
            "accepted_events": sum(loop.stats.aci_accepted for loop in loops),
            "rejected_events": sum(loop.stats.aci_rejected for loop in loops),
            "world_mutations": sum(loop.stats.aci_world_mutations for loop in loops),
        },
        "legacy_inbox": {
            "accepted": sum(loop.stats.inbox_accepted for loop in loops),
            "rejected": sum(loop.stats.inbox_rejected for loop in loops),
        },
    }


def network_result(agent: AgentSpec) -> dict:
    strict_profile = agent.network_profile in {
        "offline",
        "target-only",
        "fully-offline",
    }
    enforcement = {
        "fully-offline": "docker-none",
        "offline": "docker-none",
        "target-only": "docker-internal-network",
    }.get(agent.network_profile)
    return {
        "profile": agent.network_profile,
        "enforcement": (
            enforcement
            if enforcement
            else "advisory"
            if agent.network_profile == "benchmark-only" or strict_profile
            else "unrestricted"
        ),
    }


def _bounded(value: object, limit: int = MAX_SUMMARY_TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def run_summary(
    *,
    world: WorldRepository,
    progress: ProgressLedger,
    run_dir: Path,
    outcome: dict,
    agent_report_path: Path | None = None,
) -> dict:
    """Deterministic end-of-run summary of facts, intents and exploration.

    Written whether or not the Agent produced its own narrative, so a run stopped
    by the soft time limit still leaves usable state behind.
    """
    snapshot = world.snapshot
    facts = [
        {
            "kind": kind,
            "id": record.id,
            "type": getattr(record, "type", ""),
            "trust": record.provenance.epistemic_status,
            "detail": _bounded(
                getattr(record, "statement", None)
                or getattr(record, "content", None)
                or getattr(record, "uri", None)
                or getattr(record, "scope", None)
                or ""
            ),
        }
        for kind, store in (
            ("observation", snapshot.observations),
            ("capability", snapshot.capabilities),
            ("artifact", snapshot.artifacts),
        )
        for record in store.values()
        if not is_bookkeeping_record(record)
    ][:MAX_SUMMARY_ITEMS]
    intents = [
        {
            "id": record.id,
            "type": record.type,
            "status": record.status,
            "target": record.target,
            "skill_id": record.skill_id,
        }
        for record in snapshot.actions.values()
    ][:MAX_SUMMARY_ITEMS]
    report_path = agent_report_path or (run_dir / "agent.summary.md")
    report_text = ""
    if report_path.is_symlink():
        # The workspace is container-writable; a symlinked report is hostile
        # input and reads as absent, never as a host file's content.
        report_text = ""
    elif report_path.is_file():
        from ..secureio import read_regular_text_prefix

        try:
            report_text = read_regular_text_prefix(
                report_path, max_bytes=MAX_AGENT_REPORT_READ_BYTES
            )[:MAX_AGENT_REPORT_CHARS]
        except OSError:
            report_text = ""
    summary = {
        "schema_version": "harness/summary/v1",
        "outcome": outcome,
        "facts": facts,
        "intents": intents,
        "exploration": {
            "model_calls": progress.model_calls,
            "tool_calls": progress.tool_calls,
            "failed_skills": dict(progress.failed_skill_counts),
            "submissions_accepted": progress.accepted_submissions,
            "submissions_rejected": progress.rejected_submissions,
            "blocked_subgoals": list(progress.blocked_subgoals)[:MAX_SUMMARY_ITEMS],
            "no_progress_count": progress.no_progress_count,
            "wall_time_expired": progress.wall_time_expired,
        },
        "agent_report": {
            "path": report_path.name,
            "present": bool(report_text),
            "text": report_text,
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary
