from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


WorldObjectKind = Literal[
    "entity",
    "relation",
    "observation",
    "artifact",
    "capability",
    "hypothesis",
    "goal",
    "action",
    "constraint",
    "failure",
]

# Canonical mapping from a World object kind to its WorldSnapshot collection
# field. Every consumer that resolves kind -> snapshot field (reducer, SQLite
# store, planner, focus) uses this table instead of restating it.
KIND_COLLECTIONS: dict[str, str] = {
    "entity": "entities",
    "relation": "relations",
    "observation": "observations",
    "artifact": "artifacts",
    "capability": "capabilities",
    "hypothesis": "hypotheses",
    "goal": "goals",
    "action": "actions",
    "constraint": "constraints",
    "failure": "failures",
}


class StateSelector(BaseModel):
    """A declarative reference to a World record at a minimum trust level.

    Selectors are the shared vocabulary for skill preconditions/outputs and for
    goal target states. They live in the World layer so both domain records
    (``Goal.required_states``) and planner-facing skill metadata can reuse one
    validated type without a reverse import.
    """

    model_config = ConfigDict(extra="forbid")

    kind: WorldObjectKind
    type: str = Field(min_length=1)
    scope: str | None = None
    minimum_trust: Literal["claim", "evidence", "verified"] = "claim"


class Provenance(BaseModel):
    actor: str | None = None
    source: str | None = None
    event_id: str | None = None
    source_event_id: str | None = None
    epistemic_status: Literal["claim", "evidence", "verified", "unclassified"] = "unclassified"


# Epistemic trust ordering over ``Provenance.epistemic_status``. One shared
# lattice: the focus builder, planner selectors and the goal-frontier projection
# must agree on it or they silently rank the same state differently.
TRUST_RANK: dict[str, int] = {"unclassified": 1, "claim": 1, "evidence": 2, "verified": 3}

# Observation types the Harness writes for its own bookkeeping (one per tool
# execution). Projections that surface semantic facts exclude these so per-step
# noise does not crowd out real findings.
BOOKKEEPING_OBSERVATION_TYPES = frozenset({"tool.execution"})


def is_bookkeeping_record(record: object) -> bool:
    """True when a record is Harness bookkeeping rather than a semantic fact."""
    return getattr(record, "type", None) in BOOKKEEPING_OBSERVATION_TYPES


def agent_actor(agent_id: str) -> str:
    """Canonical actor string for an agent's authored records."""
    return f"agent:{agent_id}"


def is_agent_actor(actor: str | None) -> bool:
    """True when a record/event actor is the agent (bare or with an agent id)."""
    return actor == "agent" or str(actor or "").startswith("agent:")


class WorldRecord(BaseModel):
    provenance: Provenance = Field(default_factory=Provenance)
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    supersedes: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_temporal_window(self) -> Self:
        for name in ("observed_at", "valid_from", "expires_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if (
            self.valid_from is not None
            and self.expires_at is not None
            and self.expires_at < self.valid_from
        ):
            raise ValueError("expires_at must be greater than or equal to valid_from")
        for name in ("supersedes", "contradicts"):
            links = getattr(self, name)
            if any(not link for link in links) or len(set(links)) != len(links):
                raise ValueError(f"{name} must contain unique non-empty IDs")
        return self

    def is_valid_at(self, at: datetime | None = None) -> bool:
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("validity check time must be timezone-aware")
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return self.expires_at is None or moment <= self.expires_at


class Entity(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Relation(WorldRecord):
    id: str
    source: str
    target: str
    type: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Observation(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    content: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None


class Artifact(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    uri: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Capability(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    subject: str | None = None
    scope: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(WorldRecord):
    id: str
    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["open", "supported", "refuted", "superseded"] = "open"
    attributes: dict[str, Any] = Field(default_factory=dict)


class Goal(WorldRecord):
    id: str
    description: str = Field(min_length=1)
    status: Literal["pending", "active", "completed", "failed", "abandoned"] = "pending"
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    parent_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Declarative target states. When set, the goal-driven frontier only proposes
    # skills that (transitively) produce one of these states; an empty list keeps
    # the legacy "all enabled skills" behavior.
    required_states: list[StateSelector] = Field(default_factory=list)


class ActionRecord(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    status: Literal["planned", "running", "succeeded", "failed", "unknown"] = "unknown"
    target: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def skill_id(self) -> str | None:
        """Skill this action executed, when its intent declared one."""
        value = (self.attributes or {}).get("skill_id")
        return value if isinstance(value, str) and value else None


class Constraint(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: Literal["active", "satisfied", "violated", "expired"] = "active"
    scope: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Failure(WorldRecord):
    id: str
    type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    action_id: str | None = None
    recoverable: bool | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


WorldObject = (
    Entity
    | Relation
    | Observation
    | Artifact
    | Capability
    | Hypothesis
    | Goal
    | ActionRecord
    | Constraint
    | Failure
)
class WorldEvent(BaseModel):
    schema_version: Literal["harness/world/v1"] = "harness/world/v1"
    id: str = Field(default_factory=lambda: _id("wevt"))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: WorldObjectKind
    op: Literal["upsert", "remove"] = "upsert"
    object: dict[str, Any]
    actor: str = "harness"
    source_event_id: str | None = None


class WorldSnapshot(BaseModel):
    schema_version: Literal["harness/world/v1"] = "harness/world/v1"
    revision: int = 0
    entities: dict[str, Entity] = Field(default_factory=dict)
    relations: dict[str, Relation] = Field(default_factory=dict)
    observations: dict[str, Observation] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    hypotheses: dict[str, Hypothesis] = Field(default_factory=dict)
    goals: dict[str, Goal] = Field(default_factory=dict)
    actions: dict[str, ActionRecord] = Field(default_factory=dict)
    constraints: dict[str, Constraint] = Field(default_factory=dict)
    failures: dict[str, Failure] = Field(default_factory=dict)
