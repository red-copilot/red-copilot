from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..models import ObjectiveSpec


class BenchmarkCase(BaseModel):
    id: str = Field(min_length=1)
    benchmark: str = Field(min_length=1)
    domain: str = "general"
    difficulty: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkTarget(BaseModel):
    id: str = Field(min_length=1)
    address: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkSession(BaseModel):
    id: str = Field(min_length=1)
    benchmark: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    objective: ObjectiveSpec
    targets: list[BenchmarkTarget] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Submission(BaseModel):
    type: str = Field(min_length=1)
    value: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubmissionResult(BaseModel):
    accepted: bool
    score_delta: float = 0.0
    completed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    success: bool
    score: float = 0.0
    message: str | None = None
    milestones: dict[str, bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class AdapterSessionState:
    """Per-session bookkeeping shared by benchmark adapters.

    Adapters keep the provisioned case, every submission result seen so far
    (``evaluate`` aggregates them) and a closed flag so teardown stays
    idempotent. Adapter-specific state (gateway session ids, hint flags) lives
    in ``extras``.
    """

    case: BenchmarkCase
    submissions: list[SubmissionResult] = field(default_factory=list)
    closed: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


class BenchmarkAdapter(Protocol):
    async def discover(self) -> list[BenchmarkCase]: ...

    async def provision(self, case: BenchmarkCase) -> BenchmarkSession: ...

    async def submit(
        self,
        session: BenchmarkSession,
        submission: Submission,
    ) -> SubmissionResult: ...

    async def evaluate(self, session: BenchmarkSession) -> EvaluationResult: ...

    async def teardown(self, session: BenchmarkSession) -> None: ...
