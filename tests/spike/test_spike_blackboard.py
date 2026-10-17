"""Blackboard grammar, merge semantics and clobber detection."""

from __future__ import annotations

from pathlib import Path

from spike_swarm.blackboard import (
    BoardWriter,
    InboxRead,
    crossed_sessions,
    flag_candidates,
    merge_reads,
    parse_line,
    read_inbox,
    render_graph,
)


def _snapshot(*reads: InboxRead, round: int = 1):
    return merge_reads(list(reads), round=round)


def test_parse_line_accepts_the_documented_grammar() -> None:
    fact = parse_line(session="s1", round=1, raw="fact: port 80 open | evidence: nmap | confidence: high")
    assert fact is not None and fact.kind == "fact"
    assert fact.text == "port 80 open"
    assert fact.evidence == "nmap"
    assert fact.confidence == "high"

    intent = parse_line(
        session="s1", round=1, raw="intent: brute force login | target: 10.0.0.1 | status: doing"
    )
    assert intent is not None and intent.status == "doing" and intent.target == "10.0.0.1"

    for raw in ("question: what is the admin path?", "flag: flag{abc}", "note: watch the rate limit"):
        parsed = parse_line(session="s1", round=1, raw=raw)
        assert parsed is not None and not parsed.is_unparsed

    assert parse_line(session="s1", round=1, raw="   ") is None


def test_parse_line_files_malformed_input_as_unparsed() -> None:
    cases = {
        "totally free text": "unknown_kind",
        "fact: no fields here": "bad_confidence",
        "fact: port 80 open | evidence nmap | confidence: high": "malformed_field",
        "intent: brute force | target: x | status: maybe": "bad_intent_status",
        "fact: | evidence: nmap | confidence: high": "no_text",
    }
    for raw, reason in cases.items():
        parsed = parse_line(session="s1", round=1, raw=raw)
        assert parsed is not None and parsed.is_unparsed, raw
        assert parsed.reason == reason, raw


def test_merge_dedupes_facts_and_ranks_corroboration() -> None:
    reads = [
        InboxRead("s1", 1, ("fact: port 80 open | evidence: nmap | confidence: medium",)),
        InboxRead("s2", 1, ("fact: PORT 80 OPEN | evidence: curl | confidence: high",)),
        InboxRead("s3", 1, ("fact: ssh is closed | evidence: nc | confidence: low",)),
    ]
    snapshot = _snapshot(*reads)
    assert len(snapshot.facts) == 2
    top = snapshot.facts[0]
    assert top.corroborated
    assert top.sessions == ("s1", "s2")
    assert top.first_session == "s1"
    # The most confident statement wins the merge.
    assert top.evidence == "curl" and top.confidence == "high"


def test_merge_advances_intent_status_and_dedupes_questions() -> None:
    reads = [
        InboxRead("s1", 1, ("intent: brute force login | target: 10.0.0.1 | status: open",)),
        InboxRead("s2", 2, ("intent: brute force login | target: 10.0.0.1 | status: done",)),
        InboxRead("s3", 2, ("question: which vhost?",)),
        InboxRead("s1", 2, ("question: which vhost?",)),
    ]
    snapshot = _snapshot(*reads, round=2)
    assert len(snapshot.intents) == 1
    assert snapshot.intents[0].status == "done"
    assert snapshot.intents[0].sessions == ("s1", "s2")
    assert len(snapshot.questions) == 1
    assert len(crossed_sessions(snapshot)) == 0


def test_render_is_byte_identical_for_identical_input() -> None:
    reads = [
        InboxRead("s1", 1, ("fact: a | evidence: e | confidence: low", "junk", "flag: flag{x}")),
        InboxRead("s2", 2, ("question: why?",)),
    ]
    first = _snapshot(*reads, round=2)
    second = _snapshot(*reads, round=2)
    kwargs = {"challenge": "WEB-1", "goal_lines": ["- goal:WEB-1 [active] x"], "sessions": ["s2", "s1"]}
    assert render_graph(first, **kwargs) == render_graph(second, **kwargs)
    text = render_graph(first, **kwargs)
    assert "flag:flag{x}" not in text  # a parsed flag renders as a bullet, not a raw line
    assert "junk" in text  # unparsed lines survive verbatim
    assert "- [s1 round 1] junk (reason: unknown_kind)" in text


def test_flag_candidates_dedupe_and_strip_quoting() -> None:
    reads = [
        InboxRead("s1", 1, ("flag: flag{a}",)),
        InboxRead("s2", 2, ("flag: `flag{a}`",)),
        InboxRead("s3", 2, ("flag: flag{b}",)),
    ]
    assert flag_candidates(_snapshot(*reads, round=2)) == ["flag{a}", "flag{b}"]


def test_read_inbox_uses_the_whole_file_as_the_position(tmp_path: Path) -> None:
    path = tmp_path / "s1.md"
    path.write_text("fact: a | evidence: e | confidence: low\n", encoding="utf-8")
    read = read_inbox(session="s1", path=path, round=3)
    assert read.round == 3 and len(read.lines) == 1
    missing = read_inbox(session="s2", path=tmp_path / "nope.md", round=3)
    assert missing.lines == ()


def test_board_writer_publishes_snapshots_and_detects_a_clobber(tmp_path: Path) -> None:
    writer = BoardWriter(graph_path=tmp_path / "graph.md", snapshots_dir=tmp_path / "snapshots")
    first = writer.publish(round=1, text="first render " * 10)
    assert first["incident"] is None
    assert (tmp_path / "snapshots" / str(first["snapshot"])).exists()

    # An agent should never write this file; a shrink must be visible.
    (tmp_path / "graph.md").write_text("oops", encoding="utf-8")
    second = writer.publish(round=2, text="second render " * 10)
    incident = second["incident"]
    assert isinstance(incident, dict) and incident["kind"] == "clobber"
    assert writer.clobber_incidents == 1

    (tmp_path / "graph.md").write_text("a much longer unexpected rewrite " * 10, encoding="utf-8")
    third = writer.publish(round=3, text="third render " * 10)
    incident = third["incident"]
    assert isinstance(incident, dict) and incident["kind"] == "modified"
    assert writer.clobber_incidents == 1
    assert writer.renders == 3
