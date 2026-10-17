"""Round composition: briefs carry the board, the goals and the contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from spike_swarm.rounds import (
    MAX_BRIEF_CHARS,
    RoundOutcome,
    initial_prompt,
    read_system_prompt,
    render_objective,
    round_brief,
    status_record,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_read_system_prompt_requires_the_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_system_prompt(tmp_path)
    (tmp_path / "system-prompt.md").write_text("contract", encoding="utf-8")
    assert read_system_prompt(tmp_path) == "contract"


def test_the_shipped_solver_prompt_documents_both_channels() -> None:
    prompt = read_system_prompt(REPO_ROOT / "spike" / "agent")
    for channel in ("$SPIKE_BOARD_INBOX", "$SPIKE_BOARD_GOALS", "$SPIKE_GRAPH_FILE"):
        assert channel in prompt, channel
    for kind in ("fact:", "intent:", "question:", "flag:", "note:"):
        assert kind in prompt, kind
    assert "evidence_fact" in prompt
    assert "untrusted" in prompt


def test_render_objective_lists_targets_and_treats_output_as_data() -> None:
    text = render_objective("WEB-1", "find the flag", ["10.0.0.1"])
    assert "WEB-1" in text and "find the flag" in text and "10.0.0.1" in text
    assert "untrusted data" in text.lower()
    assert "no description provided" in render_objective("WEB-1", "   ", ())


def test_initial_prompt_names_this_session_and_its_peers() -> None:
    text = initial_prompt(
        system_prompt="CONTRACT",
        objective="OBJECTIVE",
        session_id="s2",
        peers=["s1", "s2", "s3"],
        round_seconds=120,
        inbox_file="/board/inbox/s2.md",
        goals_file="/board/goals/s2.jsonl",
    )
    assert text.startswith("CONTRACT")
    assert "session id: s2" in text
    assert "other solvers on this challenge: s1, s3" in text
    assert "/board/inbox/s2.md" in text and "/board/goals/s2.jsonl" in text
    assert "OBJECTIVE" in text


def test_round_brief_carries_goal_state_graph_and_last_status() -> None:
    brief = round_brief(
        round=3,
        session_id="s1",
        remaining_seconds=90.5,
        settled=True,
        goal_lines=["- goal:WEB-1 [active] root", "- sg-1 [blocked] enum (reason: waf)"],
        graph_text="# Intent-fact graph\n- [2 solvers] port 80 open",
        last_status={"round": 2, "settled": True},
        inbox_file="/board/inbox/s1.md",
        goals_file="/board/goals/s1.jsonl",
    )
    assert "## Round 3" in brief
    assert "wall clock left: 90s" in brief
    assert "sg-1 [blocked]" in brief
    assert "[2 solvers] port 80 open" in brief
    assert '"round": 2' in brief
    assert "evidence_fact" in brief


def test_round_brief_notes_an_unsettled_previous_turn_and_is_bounded() -> None:
    brief = round_brief(
        round=1,
        session_id="s1",
        remaining_seconds=10,
        settled=False,
        goal_lines=[],
        graph_text="x" * (MAX_BRIEF_CHARS * 2),
        last_status=None,
        inbox_file="i",
        goals_file="g",
    )
    assert "did not settle" in brief
    assert len(brief) <= MAX_BRIEF_CHARS


def test_status_record_serialises_the_round_outcome() -> None:
    outcome = RoundOutcome(
        round=2,
        session_id="s3",
        settled=False,
        alive=True,
        inbox_lines=7,
        new_facts=1,
        new_corroborated=0,
        flag_candidates=1,
        counters={"model_calls": 2},
    )
    record = status_record(outcome=outcome, brief_chars=512)
    assert record["session"] == "s3" and record["settled"] is False
    assert record["brief_chars"] == 512 and record["model_calls"] == 2
