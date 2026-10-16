"""Challenge selection: easiest first, deterministic, fail-closed."""

from __future__ import annotations

import pytest

from spike_swarm.models import (
    Challenge,
    SelectionError,
    challenge_from_sdk,
    difficulty_rank,
    select_challenges,
)


class FakeChallenge:
    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_difficulty_rank_orders_labels_and_numbers() -> None:
    assert difficulty_rank("trivial") < difficulty_rank("easy")
    assert difficulty_rank("easy") < difficulty_rank("medium")
    assert difficulty_rank("medium") < difficulty_rank("hard")
    assert difficulty_rank(2) == 2.0
    assert difficulty_rank("2") == 2.0


def test_difficulty_rank_prefers_the_longest_label_match() -> None:
    # "medium" is a substring of "easy-medium"; the specific label must win.
    assert difficulty_rank("easy-medium") == 2.0
    assert difficulty_rank("Medium Hard") == 4.0


def test_difficulty_rank_unknown_lands_in_the_middle() -> None:
    assert difficulty_rank("unlabelled") == difficulty_rank(None)
    assert difficulty_rank("unlabelled") == 3.0


def _challenge(code: str, **kwargs: object) -> Challenge:
    return Challenge(code=code, **kwargs)  # type: ignore[arg-type]


def test_selection_takes_the_easiest_unfinished_first() -> None:
    challenges = [
        _challenge("HARD-1", difficulty="hard", total_score=500),
        _challenge("EASY-1", difficulty="easy", total_score=100),
        _challenge("DONE-1", difficulty="trivial", is_completed=True),
        _challenge("MED-1", difficulty="medium", total_score=300),
        _challenge("MED-2", difficulty="medium", total_score=200),
    ]
    selected = select_challenges(challenges, count=3)
    assert [item.code for item in selected] == ["EASY-1", "MED-2", "MED-1"]


def test_selection_fails_closed_when_too_few_challenges() -> None:
    with pytest.raises(SelectionError):
        select_challenges([_challenge("ONLY-1", difficulty="easy")], count=3)


def test_selection_honours_explicit_order() -> None:
    challenges = [_challenge("A"), _challenge("B"), _challenge("C"), _challenge("D")]
    selected = select_challenges(challenges, count=3, overrides=["C", "A"])
    assert [item.code for item in selected] == ["C", "A"]


def test_selection_rejects_unknown_duplicate_and_oversized_overrides() -> None:
    challenges = [_challenge("A"), _challenge("B")]
    with pytest.raises(SelectionError):
        select_challenges(challenges, count=2, overrides=["A", "ZZZ"])
    with pytest.raises(SelectionError):
        select_challenges(challenges, count=2, overrides=["A", "A"])
    with pytest.raises(SelectionError):
        select_challenges(challenges, count=1, overrides=["A", "B"])


def test_challenge_from_sdk_maps_the_fields_the_sweep_needs() -> None:
    raw = FakeChallenge(
        unique_code="WEB-001",
        description="find the flag",
        difficulty="easy",
        level=1,
        total_score=100,
        flag_count=2,
        correct_flag_count=1,
        is_completed=False,
    )
    challenge = challenge_from_sdk(raw)
    assert challenge.code == "WEB-001"
    assert challenge.remaining_flags == 1
    assert challenge.difficulty_label == "easy"
    assert challenge.selection_key[0] == 1.0


def test_challenge_from_sdk_requires_a_code() -> None:
    with pytest.raises(SelectionError):
        challenge_from_sdk(FakeChallenge(description="no code"))
