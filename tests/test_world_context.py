"""The Agent-facing world projection keeps ruled-out paths visible."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from harness.world.context import WorldContextBuilder
from harness.world.models import (
    ActionRecord,
    Goal,
    Hypothesis,
    Observation,
    Provenance,
    WorldSnapshot,
)


def _snapshot(*hypotheses: Hypothesis) -> WorldSnapshot:
    snapshot = WorldSnapshot()
    snapshot.goals["goal-1"] = Goal(
        id="goal-1",
        description="Find the flag on the authorized target",
        status="active",
    )
    for hypothesis in hypotheses:
        snapshot.hypotheses[hypothesis.id] = hypothesis
    return snapshot


def _hypothesis(
    identifier: str,
    *,
    status: str,
    observed_at: datetime | None = None,
) -> Hypothesis:
    return Hypothesis(
        id=identifier,
        statement=f"statement for {identifier}",
        confidence=0.5,
        status=status,
        observed_at=observed_at,
    )


def test_ruled_out_hypotheses_are_rendered() -> None:
    snapshot = _snapshot(
        _hypothesis("h-open", status="open"),
        _hypothesis("h-refuted", status="refuted"),
        _hypothesis("h-superseded", status="superseded"),
    )

    rendered = WorldContextBuilder().render(snapshot)

    assert "## Ruled out" in rendered
    ruled_out = rendered.split("## Ruled out", 1)[1].split("## Artifacts", 1)[0]
    assert "h-refuted" in ruled_out
    assert "h-superseded" in ruled_out
    assert "h-open" not in ruled_out


def test_projection_without_ruled_out_paths_carries_no_section() -> None:
    snapshot = _snapshot(_hypothesis("h-open", status="open"))

    rendered = WorldContextBuilder().render(snapshot)

    assert "## Open hypotheses" in rendered
    assert "## Ruled out" not in rendered


def test_ruled_out_section_is_bounded_and_reports_omissions() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hypotheses = [
        _hypothesis(
            f"h-{index:02d}",
            status="refuted",
            observed_at=base + timedelta(minutes=index),
        )
        for index in range(12)
    ]
    builder = WorldContextBuilder(max_ruled_out=5)

    rendered = builder.render(_snapshot(*hypotheses))

    ruled_out = rendered.split("## Ruled out", 1)[1].split("## Artifacts", 1)[0]
    assert "h-11" in ruled_out
    assert "h-00" not in ruled_out
    assert "7 more ruled-out hypotheses omitted" in ruled_out


def test_budget_lines_render_without_focus_or_candidate_actions() -> None:
    """The Agent must be able to read what is left without spending a turn."""
    builder = WorldContextBuilder()
    snapshot = _snapshot()

    rendered = builder.render(
        snapshot,
        budget=["wall_time 62% left (11m10s of 30m20s)", "tokens 80% left"],
    )

    lines = rendered.splitlines()
    budget_index = next(index for index, line in enumerate(lines) if line.startswith("budget:"))
    assert lines[budget_index] == "budget: wall_time 62% left (11m10s of 30m20s)"
    assert lines[budget_index + 1] == "budget: tokens 80% left"
    assert "## Focus" not in rendered
    assert "subgoal:" not in rendered
    assert "Next focus" not in rendered


def test_rendering_without_budget_has_no_focus_or_subgoal() -> None:
    builder = WorldContextBuilder()
    snapshot = _snapshot()

    rendered = builder.render(snapshot)

    assert "budget:" not in rendered
    assert "## Focus" not in rendered
    assert "subgoal:" not in rendered
    assert "Next focus" not in rendered


def _crowded_snapshot(
    *, bookkeeping: int = 30, findings: int = 3
) -> WorldSnapshot:
    """A run shape from the 2026-10-16 sweep: noise first, findings buried."""
    from datetime import UTC, datetime

    snapshot = _snapshot()
    stamp = datetime(2026, 10, 16, 10, 50, tzinfo=UTC)
    for index in range(bookkeeping):
        snapshot.observations[f"obs:tool:call-{index:03d}"] = Observation(
            id=f"obs:tool:call-{index:03d}",
            type="tool.execution",
            content={"status": "failed" if index % 10 == 0 else "succeeded", "tool": "bash"},
            confidence=1.0,
            source="harness.tool",
            provenance=Provenance(
                actor="harness",
                source="harness.tool",
                event_id=f"wevt_{index:032d}",
                source_event_id=f"pi-rpc:deadbeef:{index}:0",
                epistemic_status="evidence",
            ),
            observed_at=stamp,
        )
    for index in range(findings):
        snapshot.observations[f"finding-{index}"] = Observation(
            id=f"finding-{index}",
            type="finding",
            content={"detail": f"semantic finding number {index}"},
            confidence=0.9,
            provenance=Provenance(actor="agent:solver", epistemic_status="claim"),
            observed_at=stamp,
        )
    for index in range(20):
        snapshot.actions[f"action:call-{index:03d}"] = ActionRecord(
            id=f"action:call-{index:03d}",
            type="bash",
            status="failed" if index == 3 else "succeeded",
            attributes={
                "duration_ms": 1000 + index,
                "provenance": {"actor": "agent:solver", "event_id": "wevt_x"},
            },
        )
    return snapshot


def test_bookkeeping_does_not_crowd_out_semantic_findings() -> None:
    builder = WorldContextBuilder()
    snapshot = _crowded_snapshot()

    rendered = builder.render(snapshot)

    for index in range(3):
        assert f"finding-{index}" in rendered
    assert "30 tool executions omitted; 3 failed" in rendered
    assert "obs:tool:call-000" not in rendered


def test_projection_drops_provenance_identifiers() -> None:
    builder = WorldContextBuilder()
    snapshot = _crowded_snapshot(bookkeeping=1)

    rendered = builder.render(snapshot)

    assert "wevt_" not in rendered
    assert "pi-rpc:" not in rendered
    assert "epistemic_status" in rendered


def test_actions_render_as_a_tally_with_a_bounded_tail() -> None:
    builder = WorldContextBuilder()
    snapshot = _crowded_snapshot(bookkeeping=1)

    rendered = builder.render(snapshot)

    assert "20 recorded: 1 failed, 19 succeeded" in rendered
    assert "12 earlier calls omitted" in rendered
    assert rendered.count("[bash]") >= 8
