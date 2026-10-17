"""Replay historical control excerpts through the v5 thin ledger."""
from __future__ import annotations

import json
from pathlib import Path

from harness.progress import ProgressLedger
from harness.session import AgentEvent

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "control_replay"


def _read_events(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def _replay(name: str) -> ProgressLedger:
    progress = ProgressLedger()
    for index, record in enumerate(_read_events(name)):
        progress.record_event(
            AgentEvent(
                type=record["type"],
                data=record["data"],
                event_id=f"replay-{index}",
            )
        )
    return progress


def test_successful_replay_has_no_heuristic_control_state() -> None:
    progress = _replay("success_generic_families.jsonl")

    assert progress.tool_calls == 4
    assert not hasattr(progress, "repetition_action")
    assert not hasattr(progress, "tool_family_failures")


def test_failed_replay_has_no_heuristic_control_state() -> None:
    progress = _replay("failure_family.jsonl")

    assert progress.tool_calls == 3
    assert progress.schema_version == "harness/progress/v5"
    assert not hasattr(progress, "steered_tool_families")
