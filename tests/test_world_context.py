"""The Agent-facing world projection keeps ruled-out paths visible."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from harness.world.context import WorldContextBuilder
from harness.world.models import Goal, Hypothesis, WorldSnapshot


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
