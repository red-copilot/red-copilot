from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.runtime import bootstrap_solver
from harness.runtime.solver_profile import SolverProfile
from harness.world import Entity, Goal, SQLiteWorldRepository


def test_bootstrap_solver_initializes_state_and_targets(tmp_path: Path) -> None:
    trace = Mock()
    goal = Goal(id="goal:example", description="Find evidence", status="active", priority=1.0)
    target = Entity(id="target:one", type="benchmark.target", attributes={"address": "localhost"})
    runtime = bootstrap_solver(
        run_dir=tmp_path,
        trace=trace,
        goal=goal,
        actor="agent:sample",
        context_query=goal.description,
        skills_root=tmp_path / "nonexistent-skills",
        targets=[target],
        target_actor="benchmark:example",
    )
    assert runtime.progress.active_goal == goal.id
    assert runtime.loop.world is runtime.world
    assert runtime.loop.progress is runtime.progress
    assert (tmp_path / "progress.json").is_file()
    assert (tmp_path / "world.context.txt").is_file()
    assert (tmp_path / "world.context.txt").read_text(encoding="utf-8")
    assert runtime.world.snapshot.revision >= 2
    assert any(event.actor == "benchmark:example" for event in runtime.world.events())
    reachability = runtime.world.snapshot.capabilities.get("reachability:target:one")
    assert reachability is not None
    assert reachability.type == "network.reachability"
    assert reachability.scope == "localhost"
    assert reachability.provenance.epistemic_status == "evidence"


def test_bootstrap_solver_enables_initial_rolling_plan_for_benchmark_targets(
    tmp_path: Path,
) -> None:
    goal = Goal(
        id="goal:web-001",
        description="Enumerate web endpoints and recover the flag",
        status="active",
        priority=1.0,
    )
    target = Entity(
        id="target-1",
        type="benchmark.target",
        attributes={"address": "10.0.0.2:8080"},
    )
    runtime = bootstrap_solver(
        run_dir=tmp_path,
        trace=Mock(),
        goal=goal,
        actor="agent:pi-tsec",
        context_query=goal.description,
        skills_root=Path("skills"),
        targets=[target],
        target_actor="benchmark:tsec",
    )
    plan = runtime.loop.planner.propose(
        runtime.world.snapshot,
        runtime.loop.skills,
        progress=runtime.progress,
        horizon=3,
    )
    assert len(plan.actions) == 3
    planned_ids = {action.skill_id for action in plan.actions}
    assert planned_ids & {"web.application-enumeration", "network.service-discovery"}


def test_bootstrap_solver_uses_custom_repository_factory(tmp_path: Path) -> None:
    paths = []

    def factory(path: Path) -> SQLiteWorldRepository:
        paths.append(path)
        return SQLiteWorldRepository(path)

    goal = Goal(id="goal:factory", description="Analyze target", status="active", priority=1.0)
    bootstrap_solver(
        run_dir=tmp_path,
        trace=Mock(),
        goal=goal,
        actor="agent:sample",
        context_query=goal.description,
        skills_root=tmp_path / "nonexistent-skills",
        world_repository_factory=factory,
    )
    assert paths == [tmp_path / "world.events.jsonl"]


@pytest.mark.parametrize(
    ("profile", "context_enabled", "planner_enabled"),
    [
        (SolverProfile.PI_ONLY, False, False),
        (SolverProfile.PI_WORLD, True, False),
        (SolverProfile.PI_WORLD_HEURISTIC, True, True),
        (SolverProfile.PI_WORLD_GOAL, True, True),
    ],
)
def test_bootstrap_solver_profiles_control_agent_context_and_planner(
    tmp_path: Path,
    profile: SolverProfile,
    context_enabled: bool,
    planner_enabled: bool,
    context_query: str = "Use solver profile",
) -> None:
    goal = Goal(id="goal:profile", description=context_query, status="active", priority=1.0)
    run_dir = tmp_path / profile.value
    run_dir.mkdir()

    runtime = bootstrap_solver(
        run_dir=run_dir,
        trace=Mock(),
        goal=goal,
        actor="agent:sample",
        context_query=goal.description,
        skills_root=tmp_path / "nonexistent-skills",
        solver_profile=profile,
    )

    context = (run_dir / "world.context.txt").read_text(encoding="utf-8")
    assert bool(context) is context_enabled
    assert runtime.loop.world_context_enabled is context_enabled
    assert runtime.loop.planner_enabled is planner_enabled


def test_goal_profile_writes_frontier_block_to_context(tmp_path: Path) -> None:
    run_dir = tmp_path / "goal-frontier"
    run_dir.mkdir()
    goal = Goal(
        id="goal:frontier",
        description="Confirm a vulnerability",
        status="active",
        priority=1.0,
        required_states=[{"kind": "observation", "type": "vuln.confirmed"}],
    )
    bootstrap_solver(
        run_dir=run_dir,
        trace=Mock(),
        goal=goal,
        actor="agent:sample",
        context_query=goal.description,
        skills_root="skills/offensive",
        solver_profile=SolverProfile.PI_WORLD_GOAL,
        targets=[
            Entity(id="t1", type="benchmark.target", attributes={"address": "10.0.0.1"})
        ],
        target_actor="benchmark:demo",
    )
    context = (run_dir / "world.context.txt").read_text(encoding="utf-8")
    assert "## Intent-Fact Frontier" in context
    assert "Open goal targets" in context
    # Backward chain from the unmet target surfaces the enabled recon prerequisite.
    assert "recon.active" in context


def test_pi_only_profile_omits_frontier_block(tmp_path: Path) -> None:
    run_dir = tmp_path / "pi-only"
    run_dir.mkdir()
    goal = Goal(id="goal:plain", description="Explore", status="active", priority=1.0)
    bootstrap_solver(
        run_dir=run_dir,
        trace=Mock(),
        goal=goal,
        actor="agent:sample",
        context_query=goal.description,
        skills_root="skills/offensive",
        solver_profile=SolverProfile.PI_ONLY,
    )
    context = (run_dir / "world.context.txt").read_text(encoding="utf-8")
    assert context == ""
