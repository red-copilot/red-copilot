from __future__ import annotations

from .agent_events import (
    ActionIntentPayload,
    validate_typed_event,
)
from .extract import extract_facts
from .progress import ProgressLedger
from .session import AgentEvent
from .world import (
    ActionRecord,
    Artifact,
    Capability,
    Failure,
    Hypothesis,
    Observation,
    Provenance,
    WorldRepository,
)


def _id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex}"


# Backward-compatible import path: ActionIntent predates the schema module.
ActionIntent = ActionIntentPayload


class TypedACI:
    """Translate small typed Agent events into authoritative solver/world state.

    Agent-authored payloads are validated against the single-source schemas in
    ``agent_events``; malformed input raises before any state is touched, and
    the solver loop turns that rejection into ``aci.feedback``.
    """

    def apply(
        self,
        event: AgentEvent,
        *,
        world: WorldRepository,
        progress: ProgressLedger,
        actor: str,
    ) -> int:
        if event.type == "action.intent":
            progress.record_intent(
                ActionIntentPayload.model_validate(event.data)
            )
            return 0

        if event.type == "world.observe":
            payload = validate_typed_event(event.type, event.data)
            if payload.summary:
                progress.actual_observation = payload.summary
            world.upsert(
                "observation",
                Observation(
                    id=payload.id or _id("obs"),
                    type=payload.type,
                    content=payload.content,
                    confidence=payload.confidence,
                    source=payload.source,
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "world.hypothesis":
            payload = validate_typed_event(event.type, event.data)
            world.upsert(
                "hypothesis",
                Hypothesis(
                    id=payload.id or _id("hyp"),
                    statement=payload.statement,
                    confidence=payload.confidence,
                    status=payload.status,
                    attributes=payload.attributes,
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "world.capability":
            payload = validate_typed_event(event.type, event.data)
            world.upsert(
                "capability",
                Capability(
                    id=payload.id or _id("cap"),
                    type=payload.type,
                    subject=payload.subject,
                    scope=payload.scope,
                    attributes=payload.attributes,
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "world.artifact":
            payload = validate_typed_event(event.type, event.data)
            world.upsert(
                "artifact",
                Artifact(
                    id=payload.id or _id("artifact"),
                    type=payload.type,
                    uri=payload.uri,
                    attributes=payload.attributes,
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "world.failure":
            payload = validate_typed_event(event.type, event.data)
            world.upsert(
                "failure",
                Failure(
                    id=payload.id or _id("failure"),
                    type=payload.type,
                    message=payload.message,
                    action_id=payload.action_id,
                    recoverable=payload.recoverable,
                    attributes=payload.attributes,
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "tool.call":
            call_id = str(event.data.get("tool_call_id") or _id("tool"))
            intent = progress.action_intent
            world.upsert(
                "action",
                ActionRecord(
                    id=f"action:{call_id}",
                    type=str(event.data.get("tool") or "tool"),
                    status="running",
                    attributes={
                        "tool_call_id": call_id,
                        "intent_action_id": (
                            intent.get("action_id") if isinstance(intent, dict) else None
                        ),
                        "skill_id": (intent.get("skill_id") if isinstance(intent, dict) else None),
                    },
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            return 1

        if event.type == "tool.result":
            call_id = str(event.data.get("tool_call_id") or _id("tool"))
            failed = bool(event.data.get("is_error", False))
            world.upsert(
                "action",
                ActionRecord(
                    id=f"action:{call_id}",
                    type=str(event.data.get("tool") or "tool"),
                    status="failed" if failed else "succeeded",
                    attributes={
                        "tool_call_id": call_id,
                        "duration_ms": event.data.get("duration_ms"),
                        "failure_class": event.data.get("failure_class"),
                        "skill_id": (
                            progress.action_intent.get("skill_id")
                            if isinstance(progress.action_intent, dict)
                            else None
                        ),
                    },
                ),
                actor=actor,
                source_event_id=event.event_id,
            )
            world.upsert(
                "observation",
                Observation(
                    id=f"obs:tool:{call_id}",
                    type="tool.execution",
                    content={
                        "tool": event.data.get("tool"),
                        "tool_call_id": call_id,
                        "status": "failed" if failed else "succeeded",
                        "duration_ms": event.data.get("duration_ms"),
                        "failure_class": event.data.get("failure_class"),
                    },
                    confidence=1.0,
                    source="harness.tool",
                    provenance=Provenance(
                        source="harness.tool",
                        epistemic_status="evidence",
                    ),
                ),
                actor="harness",
                source_event_id=event.event_id,
            )
            mutations = 2
            for fact in extract_facts(event.data):
                world.upsert(
                    "observation",
                    Observation(
                        id=fact["id"],
                        type=fact["type"],
                        content=fact["content"],
                        confidence=1.0,
                        source="harness.extract",
                        provenance=Provenance(
                            source="harness.extract", epistemic_status="evidence"
                        ),
                    ),
                    actor="harness",
                    source_event_id=event.event_id,
                )
                mutations += 1
            return mutations

        return 0
