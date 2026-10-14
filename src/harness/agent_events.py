"""Single-source schemas for Agent-authored typed events (the ACI contract).

Every event type an Agent may submit through ``$HARNESS_EVENT_FILE`` has
exactly one pydantic schema in this module. ``TypedACI`` validates against
this registry, so the wire contract, the tests and the docs cannot drift
apart. Payloads are untrusted input: unknown keys are ignored (the previous
behavior read only known keys), but known keys must have the right type -
malformed events fail closed into ``aci.feedback`` instead of being coerced.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _TypedEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _new_action_id() -> str:
    return f"action_{uuid.uuid4().hex}"


class ActionIntentPayload(_TypedEventPayload):
    schema_version: Literal["harness/action-intent/v1"] = "harness/action-intent/v1"
    action_id: str = Field(default_factory=_new_action_id)
    subgoal_id: str | None = None
    skill_id: str | None = None
    tool: str | None = None
    description: str = ""
    expected_observations: list[str] = Field(default_factory=list)
    success_conditions: list[str] = Field(default_factory=list)
    replan_conditions: list[str] = Field(default_factory=list)


class WorldObservePayload(_TypedEventPayload):
    id: str | None = None
    type: str = Field(min_length=1)
    content: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    summary: str | None = None
    source: str | None = None


class WorldHypothesisPayload(_TypedEventPayload):
    id: str | None = None
    statement: str = Field(min_length=1)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    status: Literal["open", "supported", "refuted", "superseded"] = "open"
    attributes: dict[str, Any] = Field(default_factory=dict)


class WorldCapabilityPayload(_TypedEventPayload):
    id: str | None = None
    type: str = Field(min_length=1)
    subject: str | None = None
    scope: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class WorldArtifactPayload(_TypedEventPayload):
    id: str | None = None
    type: str = Field(min_length=1)
    uri: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class WorldFailurePayload(_TypedEventPayload):
    id: str | None = None
    type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    action_id: str | None = None
    recoverable: bool | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


# One schema per Agent-authored event type. ``tool.call``/``tool.result`` are
# Harness-normalized (not Agent-authored) and stay loosely typed in TypedACI.
TYPED_EVENT_SCHEMAS: dict[str, type[_TypedEventPayload]] = {
    "action.intent": ActionIntentPayload,
    "world.observe": WorldObservePayload,
    "world.hypothesis": WorldHypothesisPayload,
    "world.capability": WorldCapabilityPayload,
    "world.artifact": WorldArtifactPayload,
    "world.failure": WorldFailurePayload,
}


def validate_typed_event(event_type: str, data: dict[str, Any]) -> _TypedEventPayload:
    """Validate one Agent-authored payload against its schema.

    Raises ``pydantic.ValidationError`` (a ``ValueError``) on malformed input,
    which the solver loop already maps to a rejected ``aci.feedback``.
    """
    schema = TYPED_EVENT_SCHEMAS.get(event_type)
    if schema is None:
        raise KeyError(f"no schema registered for typed event: {event_type}")
    return schema.model_validate(data)
