from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..agent_workspace import sync_agent_workspace
from ..budget import FINALIZE_GRACE_SECONDS
from ..progress import ProgressLedger
from ..secureio import read_regular_text
from ..solver_loop import SolverLoop
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
    world_repository_factory: Callable[[Path], WorldRepository] = SQLiteWorldRepository,
    targets: Iterable[Entity] = (),
    target_actor: str | None = None,
    budget_limits: dict[str, float] | None = None,
    processed_event_ids: dict[str, str] | None = None,
    verifier_registry: VerifierRegistry | None = None,
    agent_workspace: Path | None = None,
    resume_existing: bool = False,
    seed_world: bool = True,
    context_token_budget: int | None = None,
    finalize_requests_submission: bool = False,
    submission_due_fraction: float | None = None,
) -> SolverRuntime:
    """Initialize a solver run without coupling it to a benchmark or agent.

    Run directory creation and any world-state resume/import must occur before
    this call. Target entries are written before the initial context is rendered.
    Additional sessions competing on an already seeded world pass
    ``seed_world=False`` so they only attach their own control state.
    """
    world = world_repository_factory(run_dir / "world.events.jsonl")
    if not resume_existing and seed_world:
        world.upsert("goal", goal)
    if seed_world:
        for target in targets:
            effective_actor = target_actor or actor
            world.upsert("entity", target, actor=effective_actor)
            address = (
                target.attributes.get("address") if isinstance(target.attributes, dict) else None
            )
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

    context_builder = WorldContextBuilder(token_budget=context_token_budget)
    snapshot = world.snapshot
    initial_context = context_builder.render(snapshot, query=context_query)
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
    loop = SolverLoop(
        world=world,
        progress=progress,
        run_dir=run_dir,
        trace=trace,
        actor=actor,
        context_query=context_query,
        planned_world_revision=snapshot.revision,
        context_builder=context_builder,
        summary_path=(agent_workspace / "agent.summary.md")
        if agent_workspace is not None
        else None,
        notes_dir=(agent_workspace / "notes") if agent_workspace is not None else None,
        sync_agent_views=sync_workspace,
        finalize_requests_submission=finalize_requests_submission,
        submission_due_fraction=submission_due_fraction,
        processed_event_ids=processed_event_ids,
    )
    return SolverRuntime(
        world=world,
        context_builder=context_builder,
        progress=progress,
        verifier_registry=verifier_registry,
        loop=loop,
    )
