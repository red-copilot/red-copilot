"""The spike-local goal machine, including its corroboration gate."""

from __future__ import annotations

import json
from pathlib import Path

from spike_swarm.blackboard import FactEntry
from spike_swarm.goals import (
    ACHIEVED,
    ACTIVE,
    BLOCKED,
    GoalOpRead,
    GoalStore,
    parse_goal_op,
)
from spike_swarm.models import MAX_SUBGOALS

SESSIONS = ("s1", "s2", "s3")


def _op(session: str, round: int, payload: dict[str, object]) -> GoalOpRead:
    return GoalOpRead(session=session, round=round, raw=json.dumps(payload))


def _fact(text: str, first_session: str, sessions: tuple[str, ...]) -> FactEntry:
    return FactEntry(
        text=text,
        evidence="tool",
        confidence="high",
        first_session=first_session,
        first_round=1,
        sessions=sessions,
    )


def test_parse_goal_op_marks_malformed_lines() -> None:
    assert parse_goal_op(session="s1", round=1, raw="not json").op == "malformed_op"
    assert parse_goal_op(session="s1", round=1, raw='[1, 2]').op == "malformed_op"
    parsed = parse_goal_op(
        session="s1",
        round=1,
        raw=json.dumps({"op": "propose", "id": "sg-1", "description": "x"}),
    )
    assert parsed.op == "propose" and parsed.goal_id == "sg-1"


def test_propose_applies_and_rejects_bad_inputs(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.activate_root(round=0)
    results = store.apply_reads(
        [
            _op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enumerate"}),
            _op("s1", 1, {"op": "propose", "id": "sg-1", "description": "again"}),
            _op("s2", 1, {"op": "propose", "id": "bad id!", "description": "x"}),
            _op("s2", 1, {"op": "propose", "id": "sg-2", "parent": "nope", "description": "x"}),
            _op("s2", 1, {"op": "propose", "id": "sg-3", "description": "  "}),
            _op("s3", 1, {"op": "teleport", "id": "sg-4"}),
            _op("s9", 1, {"op": "propose", "id": "sg-5", "description": "x"}),
        ],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    reasons = [(item.op, item.result, item.reason) for item in results]
    assert reasons[0] == ("propose", "applied", "")
    assert reasons[1][1:] == ("rejected", "duplicate_goal_id")
    assert reasons[2][1:] == ("rejected", "bad_goal_id")
    assert reasons[3][1:] == ("rejected", "unknown_parent")
    assert reasons[4][1:] == ("rejected", "empty_description")
    assert reasons[5][1:] == ("rejected", "unknown_op")
    assert reasons[6][1:] == ("rejected", "unknown_session")
    assert store.nodes["sg-1"].status == ACTIVE


def test_subgoal_cap_is_enforced(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    reads = [
        _op("s1", 1, {"op": "propose", "id": f"sg-{index}", "description": "x"})
        for index in range(MAX_SUBGOALS + 1)
    ]
    results = store.apply_reads(reads, round=1, facts=[], sessions=SESSIONS)
    assert sum(1 for item in results if item.result == "applied") == MAX_SUBGOALS
    assert results[-1].result == "rejected" and results[-1].reason == "subgoal_cap"


def test_achieved_requires_a_fact_a_peer_contributed_first(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.apply_reads(
        [_op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enum"})],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    same_session = [_fact("port 80 open", "s1", ("s1",))]
    rejected = store.apply_reads(
        [_op("s1", 2, {"op": "status", "id": "sg-1", "status": "achieved",
                       "evidence_fact": "port 80 open"})],
        round=2,
        facts=same_session,
        sessions=SESSIONS,
    )
    assert rejected[0].result == "rejected" and rejected[0].reason == "uncorroborated"
    assert store.nodes["sg-1"].status == BLOCKED
    assert store.nodes["sg-1"].reason == "uncorroborated"

    # Reopening the goal and citing a peer's contribution closes it.
    store.apply_reads(
        [_op("s1", 3, {"op": "status", "id": "sg-1", "status": "active"})],
        round=3,
        facts=same_session,
        sessions=SESSIONS,
    )
    peer_fact = [_fact("port 80 open", "s2", ("s1", "s2"))]
    applied = store.apply_reads(
        [_op("s1", 4, {"op": "status", "id": "sg-1", "status": "achieved",
                       "evidence_fact": "port 80 open"})],
        round=4,
        facts=peer_fact,
        sessions=SESSIONS,
    )
    assert applied[0].result == "applied"
    assert store.nodes["sg-1"].status == ACHIEVED


def test_achieved_with_unknown_evidence_is_rejected_and_parked(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.apply_reads(
        [_op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enum"})],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    results = store.apply_reads(
        [_op("s1", 2, {"op": "status", "id": "sg-1", "status": "achieved",
                       "evidence_fact": "invented"})],
        round=2,
        facts=[],
        sessions=SESSIONS,
    )
    assert results[0].reason == "unknown_evidence_fact"
    assert store.nodes["sg-1"].status == BLOCKED


def test_terminal_goals_are_immutable_and_the_root_is_not_addressable(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.activate_root(round=0)
    store.apply_reads(
        [_op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enum"})],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    store.apply_reads(
        [_op("s1", 2, {"op": "status", "id": "sg-1", "status": "abandoned"})],
        round=2,
        facts=[],
        sessions=SESSIONS,
    )
    results = store.apply_reads(
        [
            _op("s2", 3, {"op": "status", "id": "sg-1", "status": "achieved",
                          "evidence_fact": "port 80 open"}),
            _op("s2", 3, {"op": "status", "id": store.root_id, "status": "achieved",
                          "evidence_fact": "port 80 open"}),
        ],
        round=3,
        facts=[_fact("port 80 open", "s1", ("s1", "s2"))],
        sessions=SESSIONS,
    )
    assert results[0].reason == "terminal_status"
    assert results[1].reason == "unknown_goal"
    assert store.root.status == ACTIVE


def test_root_closes_only_through_the_runtime(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.activate_root(round=0)
    assert store.root.status == ACTIVE
    store.achieve_root(flag="flag{accepted}", round=4)
    assert store.root.status == ACHIEVED
    store.abandon_root(reason="window_elapsed", round=5)
    assert store.root.status == ACHIEVED  # terminal states cannot be overwritten


def test_journal_replay_reproduces_the_goal_state(tmp_path: Path) -> None:
    journal = tmp_path / "goal.jsonl"
    original = GoalStore(code="WEB-1", description="root", journal=journal)
    original.activate_root(round=0)
    original.apply_reads(
        [_op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enum"})],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    original.apply_reads(
        [_op("s2", 2, {"op": "status", "id": "sg-1", "status": "blocked", "reason": "waf"})],
        round=2,
        facts=[],
        sessions=SESSIONS,
    )
    replayed = GoalStore(code="WEB-1", description="root", journal=journal)
    assert replayed.root.status == ACTIVE
    assert replayed.nodes["sg-1"].status == BLOCKED
    assert replayed.nodes["sg-1"].reason == "waf"
    assert replayed.summary()["counts"]["blocked"] == 1


def test_render_lines_and_summary_describe_the_tree(tmp_path: Path) -> None:
    store = GoalStore(code="WEB-1", description="root", journal=tmp_path / "goal.jsonl")
    store.activate_root(round=0)
    store.apply_reads(
        [_op("s1", 1, {"op": "propose", "id": "sg-1", "description": "enum"})],
        round=1,
        facts=[],
        sessions=SESSIONS,
    )
    lines = store.render_lines()
    assert lines[0].startswith(f"- {store.root_id} [{ACTIVE}]")
    assert any("sg-1" in line and "by s1" in line for line in lines)
    counts = store.summary()["counts"]
    assert counts["subgoals"] == 1 and counts["active"] == 1
