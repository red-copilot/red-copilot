from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import WorldObjectKind


class WorldSubmission(BaseModel):
    """Untrusted Agent-to-Harness world-state submission."""

    schema_version: Literal["harness/world-submission/v1"] = "harness/world-submission/v1"
    kind: WorldObjectKind
    op: Literal["upsert", "remove"] = "upsert"
    object: dict
    source_event_id: str | None = None


class IngestError(BaseModel):
    line: int
    message: str


class IngestReport(BaseModel):
    accepted: int = 0
    rejected: int = 0
    errors: list[IngestError] = Field(default_factory=list)


def write_submission(path: Path, submission: WorldSubmission) -> None:
    """Append one submission. Intended for trusted adapters and tests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(submission.model_dump_json() + "\n")
        handle.flush()
