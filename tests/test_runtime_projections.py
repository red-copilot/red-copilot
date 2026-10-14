from pathlib import Path
from types import SimpleNamespace

from harness.progress import ProgressLedger
from harness.runtime.projections import (
    finalize_world_goal,
    network_result,
    run_summary,
    world_result,
)
from harness.world import Goal, SQLiteWorldRepository, WorldContextBuilder


def test_finalize_world_goal_and_result(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    goal = Goal(id="goal:test", description="Test objective", status="active", priority=1.0)
    world.upsert("goal", goal)
    finalize_world_goal(
        world=world,
        goal=goal,
        context_builder=WorldContextBuilder(),
        run_dir=tmp_path,
        status="finished",
        success=True,
        score=1.0,
    )
    assert world.snapshot.revision >= 2
    assert goal.status == "completed"
    assert goal.attributes["score"] == 1.0
    assert (tmp_path / "world.context.txt").is_file()

    stats = SimpleNamespace(
        aci_accepted=2,
        aci_rejected=1,
        aci_world_mutations=3,
        inbox_accepted=4,
        inbox_rejected=5,
    )
    result = world_result(
        world=world, solver_loop=SimpleNamespace(stats=stats), agent_id="test"
    )
    assert result["aci"]["accepted_events"] == 2
    assert result["legacy_inbox"]["rejected"] == 5
    assert result["agent_authored_records"] == 0


def test_network_result_preserves_profiles() -> None:
    assert network_result(SimpleNamespace(network_profile="offline", type="pi")) == {
        "profile": "offline", "enforcement": "docker-none",
    }
    assert network_result(SimpleNamespace(network_profile="benchmark-only", type="pi"))[
        "enforcement"
    ] == "advisory"
    assert network_result(SimpleNamespace(network_profile="unrestricted", type="docker"))[
        "enforcement"
    ] == "unrestricted"


def test_run_summary_captures_facts_intents_and_the_agent_report(tmp_path: Path) -> None:
    from harness.world import Artifact, Observation

    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    world.upsert("goal", Goal(id="goal:s", description="Sum", status="active", priority=1.0))
    world.upsert(
        "observation",
        Observation(id="obs:port", type="network.service", content={"port": 80}),
    )
    world.upsert("artifact", Artifact(id="art:poc", type="vuln.poc", uri="file:///poc"))
    progress = ProgressLedger(
        budget_limits={"wall_time": 10},
        wall_time_grace_seconds=180,
        failed_skill_counts={"web.application-enumeration": 2},
        replan_count=3,
        tool_calls=7,
        model_calls=5,
    )
    (tmp_path / "agent.summary.md").write_text(
        "Confirmed: port 80 open. Tried LFI, ruled out.\n", encoding="utf-8"
    )

    summary = run_summary(
        world=world,
        progress=progress,
        run_dir=tmp_path,
        outcome={"status": "timeout", "success": False, "score": 0.0},
    )

    assert summary["schema_version"] == "harness/summary/v1"
    kinds = {fact["kind"] for fact in summary["facts"]}
    assert {"observation", "artifact"} <= kinds
    assert any(fact["type"] == "network.service" for fact in summary["facts"])
    assert summary["exploration"]["failed_skills"] == {"web.application-enumeration": 2}
    assert summary["exploration"]["tool_calls"] == 7
    assert summary["agent_report"]["present"] is True
    assert "ruled out" in summary["agent_report"]["text"]
    assert (tmp_path / "summary.json").is_file()


def test_run_summary_is_written_without_an_agent_report(tmp_path: Path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    summary = run_summary(
        world=world,
        progress=ProgressLedger(),
        run_dir=tmp_path,
        outcome={"status": "finished"},
    )
    assert summary["facts"] == []
    assert summary["agent_report"] == {"path": "agent.summary.md", "present": False, "text": ""}
