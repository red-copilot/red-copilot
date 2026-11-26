"""Untrusted JSONL inboxes are bounded reads with truncate-safe cursors.

``WorldInboxCursor``, ``SubmissionInbox`` and ``HintCursor`` all poll files
incrementally. Two failure modes had to hold for all of them:

1. the poll read is bounded (the container can write the inbox files, so an
   unbounded ``read()`` is a memory-exhaustion lever), and
2. a file truncated behind the cursor resets the cursor instead of silently
   hiding every future append (an Agent that truncates its inbox must not be
   able to stop its later submissions from being consumed).
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.benchmark.base import Submission
from harness.benchmark.protocol import SubmissionInbox
from harness.hints import HintCursor, append_hint
from harness.world import SQLiteWorldRepository
from harness.world.live import WorldInboxCursor
from harness.world.protocol import WorldSubmission

# Generous relative to real inbox traffic, small enough that an unbounded
# read of the fixture below is obvious: the file holds several times this
# many bytes, so a single unbounded poll would blow straight past it.
POLL_BOUND_BYTES = 1024 * 1024


def _world_submission(value: str = "obs-1") -> str:
    payload = WorldSubmission(
        kind="observation",
        op="upsert",
        object={"id": value, "type": "note", "content": {"summary": value}},
    )
    return payload.model_dump_json()


def test_world_inbox_poll_is_bounded(tmp_path: Path) -> None:
    inbox = tmp_path / "world.inbox.jsonl"
    line = _world_submission("obs-1") + "\n"
    inbox.write_text(line * 20_000, encoding="utf-8")  # ~3 MB, ~3x the bound
    store = SQLiteWorldRepository(tmp_path / "world.db")
    cursor = WorldInboxCursor(inbox, store, actor="agent:test")

    report = cursor.poll()

    # One poll consumed at most the chunk bound and delivered something;
    # repeated polls drain the rest.
    assert report.accepted + report.rejected >= 1
    assert cursor.offset <= POLL_BOUND_BYTES + len(line)
    drained = report.accepted
    while cursor.offset < inbox.stat().st_size:
        followup = cursor.poll()
        assert followup.accepted + followup.rejected >= 1
        drained += followup.accepted + followup.rejected
    assert drained == 20_000


def test_submission_inbox_poll_is_bounded(tmp_path: Path) -> None:
    inbox = tmp_path / "submission.inbox.jsonl"
    line = json.dumps({"type": "flag", "value": "flag{a}"}) + "\n"
    inbox.write_text(line * 30_000, encoding="utf-8")  # ~1.5 MB, over the bound

    cursor = SubmissionInbox(inbox)
    submissions = cursor.poll()

    assert submissions
    assert all(isinstance(item, Submission) for item in submissions)
    assert cursor.offset <= POLL_BOUND_BYTES + len(line)


def test_world_inbox_cursor_resets_after_truncation(tmp_path: Path) -> None:
    inbox = tmp_path / "world.inbox.jsonl"
    inbox.write_text(_world_submission("obs-first") + "\n", encoding="utf-8")
    store = SQLiteWorldRepository(tmp_path / "world.db")
    cursor = WorldInboxCursor(inbox, store, actor="agent:test")

    first = cursor.poll()
    assert first.accepted == 1

    # The Agent truncates the file (its own workspace file) and appends anew.
    inbox.write_text(_world_submission("obs-second") + "\n", encoding="utf-8")

    second = cursor.poll()

    assert second.accepted == 1, "truncation behind the cursor hid the new line"


def test_submission_inbox_resets_after_truncation(tmp_path: Path) -> None:
    inbox = tmp_path / "submission.inbox.jsonl"
    inbox.write_text(
        json.dumps({"type": "flag", "value": "flag{one}"}) + "\n", encoding="utf-8"
    )
    cursor = SubmissionInbox(inbox)
    assert len(cursor.poll()) == 1

    inbox.write_text(
        json.dumps({"type": "flag", "value": "flag{two}"}) + "\n", encoding="utf-8"
    )

    submissions = cursor.poll()

    assert [item.value for item in submissions] == ["flag{two}"]


def test_hint_cursor_resets_after_truncation(tmp_path: Path) -> None:
    append_hint(tmp_path, "first hint")
    cursor = HintCursor(tmp_path / "hints.jsonl")
    assert [hint.text for hint in cursor.poll()] == ["first hint"]

    # The run root is operator-owned, but a resumed run reuses the file; a
    # truncating editor must not wedge the cursor either.
    (tmp_path / "hints.jsonl").write_text(
        json.dumps(
            {"id": "hint_0001", "text": "second hint", "created_at": "t"}
        )
        + "\n",
        encoding="utf-8",
    )

    hints = cursor.poll()

    assert [hint.text for hint in hints] == ["second hint"]
