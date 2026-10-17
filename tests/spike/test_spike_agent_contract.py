"""The shipped agent contract must match the code that reads it.

Nothing here runs a model: it pins the documented inbox grammar, the documented
goal ops and the documented container paths to the parser and the mount plan, so
a prompt that teaches a grammar the runtime rejects fails the build instead of
failing a live sweep.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from spike_swarm.blackboard import parse_line
from spike_swarm.containers import (
    CONTAINER_BOARD,
    CONTAINER_GOALS,
    CONTAINER_GRAPH,
    CONTAINER_INBOX,
    CONTAINER_KNOWLEDGE,
    CONTAINER_NOTES,
    CONTAINER_WORKDIR,
)
from spike_swarm.goals import parse_goal_op

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "spike" / "agent"
PROMPT = AGENT_DIR / "system-prompt.md"
PROTOCOL = AGENT_DIR / "blackboard-protocol.md"

FENCE = re.compile(r"```([a-z]*)\n(.*?)```", re.DOTALL)


def _blocks(text: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2).strip()) for match in FENCE.finditer(text)]


def test_documented_inbox_lines_parse() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    blocks = [body for language, body in _blocks(text) if language == "text"]
    assert len(blocks) == 1, "the inbox grammar block moved"
    lines = [line for line in blocks[0].splitlines() if line.strip()]
    kinds = set()
    for line in lines:
        parsed = parse_line(session="s1", round=1, raw=line)
        assert parsed is not None, line
        assert not parsed.is_unparsed, f"{line} -> {parsed.reason}"
        kinds.add(parsed.kind)
    assert kinds == {"fact", "intent", "question", "flag", "note"}


def test_documented_goal_ops_are_parseable() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    blocks = [body for language, body in _blocks(text) if language == "json"]
    assert blocks, "the goal-op block moved"
    ops = []
    for body in blocks:
        for line in body.splitlines():
            if not line.strip():
                continue
            ops.append(parse_goal_op(session="s1", round=1, raw=line))
    assert ops, "no goal ops documented"
    assert {op.op for op in ops} == {"propose", "status"}
    for op in ops:
        if op.op == "propose":
            assert op.goal_id and op.description
        else:
            assert op.goal_id and op.status


def test_the_prompt_names_the_environment_the_runtime_sets() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    for name in ("SPIKE_BOARD_INBOX", "SPIKE_BOARD_GOALS", "SPIKE_GRAPH_FILE", "SPIKE_NOTES_DIR"):
        assert f"${name}" in text, name


def test_the_protocol_documents_the_mounts_the_runtime_creates() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")
    documented = set(re.findall(r"`(/[A-Za-z0-9_./*<>-]+)`", text))
    for path in (
        CONTAINER_INBOX,
        CONTAINER_GOALS,
        CONTAINER_GRAPH,
        CONTAINER_NOTES,
        CONTAINER_WORKDIR,
    ):
        assert any(entry.startswith(path) for entry in documented), path
    assert any(entry.startswith(f"{CONTAINER_KNOWLEDGE}/") for entry in documented)
    # The board root is only ever a parent of the inbox/goals mounts.
    assert any(entry.startswith(CONTAINER_BOARD) for entry in documented)


def test_the_protocol_and_the_prompt_agree_on_the_merge_rules() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "Unparsed" in text and "verbatim" in text
    assert "most advanced status wins" in text
    assert "clobber" in text
    prompt = PROMPT.read_text(encoding="utf-8")
    for phrase in ("`achieved`", "different solver", "runtime"):
        assert phrase in prompt, phrase
    ops = [op for op in _blocks(prompt) if op[0] == "json"]
    assert ops, "the goal-op block moved"
    documented = [
        json.loads(line) for _, body in ops for line in body.splitlines() if line.strip()
    ]
    assert any(op.get("status") == "achieved" for op in documented)
