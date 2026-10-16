from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .secureio import open_regular_file

HINTS_FILE = "hints.jsonl"
MAX_HINT_TEXT_CHARS = 16_000
MAX_HINT_LINE_BYTES = 256 * 1024


@dataclass(frozen=True)
class HintRecord:
    id: str
    text: str
    created_at: str


def append_hint(run_dir: Path, text: str) -> HintRecord:
    """Operator-side append of a human hint to a run's hint inbox.

    Hints are advisory operator guidance recorded next to the trace; they are
    never a verifier verdict and cannot close the objective loop. The file is
    append-only, so a hint added while a run is in flight is absorbed the next
    time the SolverLoop polls the inbox.
    """
    path = run_dir / HINTS_FILE
    line_count = 0
    needs_leading_newline = False
    if path.exists():
        with open_regular_file(path, "r", errors="replace") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    needs_leading_newline = True
                line_count += 1
    record = HintRecord(
        id=f"hint_{line_count + 1:04d}",
        text=text.strip()[:MAX_HINT_TEXT_CHARS],
        created_at=datetime.now(UTC).isoformat(),
    )
    with open_regular_file(path, "a") as handle:
        if needs_leading_newline:
            # A hand-edited file may lack the final newline; without this the
            # next record would be concatenated onto a corrupt partial line.
            handle.write("\n")
        handle.write(
            json.dumps(record.__dict__, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        handle.flush()
    return record


class HintCursor:
    """Incrementally read operator-authored hints (trusted input, bounded reads)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.remainder = ""
        self.line_number = 0

    def poll(self) -> list[HintRecord]:
        hints: list[HintRecord] = []
        if not self.path.exists():
            return hints

        with open_regular_file(self.path, "r", errors="replace") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()

        if not chunk:
            return hints

        text = self.remainder + chunk
        lines = text.splitlines(keepends=True)
        self.remainder = ""

        for line in lines:
            if not line.endswith(("\n", "\r")):
                self.remainder = line
                continue

            self.line_number += 1
            raw = line.strip()
            if not raw or len(raw) > MAX_HINT_LINE_BYTES:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            text_value = payload.get("text")
            if not isinstance(text_value, str) or not text_value.strip():
                continue
            identifier = str(payload.get("id") or f"hint_{self.line_number:04d}")
            hints.append(
                HintRecord(
                    id=identifier[:64],
                    text=text_value[:MAX_HINT_TEXT_CHARS],
                    created_at=str(payload.get("created_at") or ""),
                )
            )
        return hints
