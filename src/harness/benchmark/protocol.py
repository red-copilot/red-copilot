from __future__ import annotations

import json
from pathlib import Path

from ..secureio import poll_regular_chunk
from .base import Submission

# One poll reads a bounded chunk of the Agent-writable inbox; the cursor
# drains the rest on later polls.
MAX_POLL_CHUNK_BYTES = 1024 * 1024


class SubmissionInbox:
    """Incrementally read Agent-proposed benchmark submissions from JSONL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.remainder = ""
        self.seen: set[tuple[str, str]] = set()
        self._file_identity: tuple[int, int] | None = None

    def poll(self) -> list[Submission]:
        if not self.path.exists():
            return []
        chunk, self.offset, self._file_identity = poll_regular_chunk(
            self.path,
            self.offset,
            chunk_bytes=MAX_POLL_CHUNK_BYTES,
            identity=self._file_identity,
        )
        if not chunk:
            return []

        text = self.remainder + chunk.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        self.remainder = ""
        submissions: list[Submission] = []
        for line in lines:
            if not line.endswith(("\n", "\r")):
                self.remainder = line
                continue
            try:
                payload = json.loads(line)
                submission = Submission.model_validate(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            key = (submission.type, submission.value)
            if key in self.seen:
                continue
            self.seen.add(key)
            submissions.append(submission)
        return submissions
