from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..secureio import poll_regular_chunk
from .protocol import IngestError, IngestReport, WorldSubmission
from .repository import WorldRepository

# One poll reads a bounded chunk of the Agent-writable inbox: an unbounded
# read is a memory lever, and the cursor drains the rest on later polls.
MAX_POLL_CHUNK_BYTES = 1024 * 1024


class WorldInboxCursor:
    """Incrementally validate and apply untrusted world-state JSONL submissions."""

    def __init__(
        self,
        path: Path,
        store: WorldRepository,
        *,
        actor: str = "agent",
    ) -> None:
        self.path = path
        self.store = store
        self.actor = actor
        self.offset = 0
        self.remainder = ""
        self.line_number = 0
        self._file_identity: tuple[int, int] | None = None

    def poll(self) -> IngestReport:
        report = IngestReport()
        if not self.path.exists():
            return report

        chunk, self.offset, self._file_identity = poll_regular_chunk(
            self.path,
            self.offset,
            chunk_bytes=MAX_POLL_CHUNK_BYTES,
            identity=self._file_identity,
        )
        if not chunk:
            return report

        text = self.remainder + chunk.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        self.remainder = ""

        for line in lines:
            if not line.endswith(("\n", "\r")):
                self.remainder = line
                continue

            self.line_number += 1
            raw = line.strip()
            if not raw:
                continue

            try:
                submission = WorldSubmission.model_validate_json(raw)
                self._apply(submission)
                report.accepted += 1
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                report.rejected += 1
                report.errors.append(IngestError(line=self.line_number, message=str(exc)))

        return report

    def _apply(self, submission: WorldSubmission) -> None:
        if submission.op == "remove":
            object_id = submission.object.get("id")
            if not isinstance(object_id, str) or not object_id:
                raise ValueError("remove submissions require object.id")
            self.store.remove(
                submission.kind,
                object_id,
                actor=self.actor,
            )
            return

        self.store.upsert(
            submission.kind,
            submission.object,
            actor=self.actor,
            source_event_id=submission.source_event_id,
        )

