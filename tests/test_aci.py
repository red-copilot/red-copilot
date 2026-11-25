from __future__ import annotations

import pytest

from harness.aci import TypedACI
from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.world import SQLiteWorldRepository


def _tool_result(*, origin: str, excerpt: str = "80/tcp   open  http") -> AgentEvent:
    return AgentEvent(
        type="tool.result",
        data={
            "tool": "bash",
            "tool_call_id": "c1",
            "is_error": False,
            "duration_ms": 5,
            "output_excerpt": excerpt,
        },
        event_id="agent-event-line:1" if origin == "agent" else "pi-rpc:1:1:0",
        origin=origin,
    )


def test_agent_authored_tool_result_is_rejected_without_world_mutation(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger(active_goal="goal:test")

    with pytest.raises(ValueError, match="cannot become harness evidence"):
        TypedACI().apply(
            _tool_result(origin="agent"),
            world=world,
            progress=progress,
            actor="agent:pi",
        )

    snapshot = world.snapshot
    assert snapshot.revision == 0
    assert snapshot.actions == {}
    assert snapshot.observations == {}


def test_harness_tool_result_remains_evidence(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger(active_goal="goal:test")

    mutations = TypedACI().apply(
        _tool_result(origin="harness"),
        world=world,
        progress=progress,
        actor="agent:pi",
    )

    snapshot = world.snapshot
    assert mutations >= 2
    assert snapshot.actions["action:c1"].status == "succeeded"
    observation = snapshot.observations["obs:tool:c1"]
    assert observation.source == "harness.tool"
    assert observation.provenance.epistemic_status == "evidence"
    assert any(
        record.source == "harness.extract"
        for record in snapshot.observations.values()
    )


def test_default_event_origin_is_harness() -> None:
    assert AgentEvent(type="tool.result", data={}).origin == "harness"
