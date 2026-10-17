"""Replay the two historical failure shapes that motivated the closing changes."""
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


def test_successful_same_family_commands_do_not_trigger_repetition() -> None:
    progress = _replay("success_generic_families.jsonl")

    assert progress.repetition_action() is None
    assert progress.steered_tool_families == {}


def test_failed_same_family_commands_trigger_one_targeted_signal() -> None:
    progress = _replay("failure_family.jsonl")

    action = progress.repetition_action()
    assert action is not None
    assert action[0] == "bash:curl"
    assert action[2] == 1
    assert action[3] == "failure_family"
