from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..agent_workspace import sync_agent_workspace
from ..budget import FINALIZE_GRACE_SECONDS
from ..cognition import GoalFrontierPlanner, frontier_context
from ..planner import HeuristicSkillPlanner, RollingHorizonPlanner
from ..progress import ProgressLedger
from ..secureio import read_regular_text
from ..skills import load_skills
from ..solver_loop import SolverLoop
from ..solver_policy import (
    DEFAULT_MAX_NO_PROGRESS,
    DEFAULT_MAX_REPLANS,
    DEFAULT_MAX_UNCONFIRMED_PROGRESS,
)
from ..trace import TraceRecorder
from ..verification import VerifierRegistry
from ..world import (
    Capability,
    Entity,
    Goal,
    SQLiteWorldRepository,
    WorldContextBuilder,
    WorldRepository,
)
from .solver_profile import (
    DEFAULT_SOLVER_PROFILE,
    SolverProfile,
    solver_profile_candidate,
    solver_profile_settings,
)

_CANDIDATE_PLANNERS = {
    "heuristic": HeuristicSkillPlanner,
    "goal-frontier": GoalFrontierPlanner,
}


def _build_planner(profile: SolverProfile) -> RollingHorizonPlanner:
    """Compose the rolling-horizon planner over the profile's candidate ranker."""
    candidate_id = solver_profile_candidate(profile)
    candidate_cls = _CANDIDATE_PLANNERS.get(candidate_id or "")
    return RollingHorizonPlanner(candidate_planner=candidate_cls() if candidate_cls else None)


@dataclass(frozen=True)
class SolverRuntime:
    world: WorldRepository
    context_builder: WorldContextBuilder
    progress: ProgressLedger
    verifier_registry: VerifierRegistry
    loop: SolverLoop


def bootstrap_solver(
    *,
    run_dir: Path,
    trace: TraceRecorder,
    goal: Goal,
    actor: str,
    context_query: str,
    skills_root: str | Path,
    world_repository_factory: Callable[[Path], WorldRepository] = SQLiteWorldRepository,
    targets: Iterable[Entity] = (),
    target_actor: str | None = None,
    budget_limits: dict[str, float] | None = None,
    processed_event_ids: dict[str, str] | None = None,
    verifier_registry: VerifierRegistry | None = None,
    agent_workspace: Path | None = None,
    resume_existing: bool = False,
    solver_profile: SolverProfile = DEFAULT_SOLVER_PROFILE,
    max_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
    max_unconfirmed_progress: int = DEFAULT_MAX_UNCONFIRMED_PROGRESS,
    max_replans: int = DEFAULT_MAX_REPLANS,
    context_token_budget: int | None = None,
) -> SolverRuntime:
    """Initialize a solver run without coupling it to a benchmark or agent.

    Run directory creation and any world-state resume/import must occur before
    this call. Target entries are written before the initial context is rendered.
    """
    world = world_repository_factory(run_dir / "world.events.jsonl")
    if not resume_existing:
        world.upsert("goal", goal)
    for target in targets:
        effective_actor = target_actor or actor
        world.upsert("entity", target, actor=effective_actor)
        address = target.attributes.get("address") if isinstance(target.attributes, dict) else None
        if target.type == "benchmark.target" or (isinstance(address, str) and address):
            scope = str(address) if isinstance(address, str) and address else target.id
            world.upsert(
                "capability",
                Capability(
                    id=f"reachability:{target.id}",
                    type="network.reachability",
                    subject=actor,
                    scope=scope,
                    attributes={"target_id": target.id},
                ),
                actor=effective_actor,
            )

    world_context_enabled, planner_enabled = solver_profile_settings(solver_profile)
    context_builder = WorldContextBuilder(
        focus_enabled=world_context_enabled,
        token_budget=context_token_budget,
    )
    skills = load_skills(skills_root)
    snapshot = world.snapshot
    if world_context_enabled:
        focus = context_builder.build_focus(snapshot, reference=context_query)
        initial_context = context_builder.render(
            snapshot,
            query=context_query,
            focus=focus,
        )
        initial_context += "\n" + frontier_context(snapshot, skills)
    else:
        initial_context = ""
    (run_dir / "world.context.txt").write_text(initial_context, encoding="utf-8")
    progress = (
        ProgressLedger.model_validate_json(
            read_regular_text(run_dir / "progress.json", max_bytes=8 * 1024 * 1024)
        )
        if resume_existing
        else ProgressLedger(
            active_goal=goal.id,
            budget_limits=budget_limits or {},
            wall_time_grace_seconds=FINALIZE_GRACE_SECONDS,
        )
    )
    verifier_registry = verifier_registry or VerifierRegistry()
    progress.write(run_dir / "progress.json")
    sync_workspace = lambda: (
        sync_agent_workspace(run_dir=run_dir, workspace=agent_workspace)
        if agent_workspace is not None
        else None
    )
    planner = _build_planner(solver_profile) if planner_enabled else None
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=run_dir,
        trace=trace,
        actor=actor,
        context_query=context_query,
        planned_world_revision=snapshot.revision,
        context_builder=context_builder,
        verifier_registry=verifier_registry,
        world_inbox_path=(agent_workspace / "world.inbox.jsonl")
        if agent_workspace is not None
        else None,
        summary_path=(agent_workspace / "agent.summary.md")
        if agent_workspace is not None
        else None,
        sync_agent_views=sync_workspace,
        skills=skills,
        planner=planner,
        planner_enabled=planner_enabled,
        world_context_enabled=world_context_enabled,
        max_no_progress=max_no_progress,
        max_unconfirmed_progress=max_unconfirmed_progress,
        max_replans=max_replans,
        processed_event_ids=processed_event_ids,
    )
    return SolverRuntime(
        world=world,
        context_builder=context_builder,
        progress=progress,
        verifier_registry=verifier_registry,
        loop=loop,
    )
