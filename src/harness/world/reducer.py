from __future__ import annotations

from .models import (
    KIND_COLLECTIONS,
    ActionRecord,
    Artifact,
    Capability,
    Constraint,
    Entity,
    Failure,
    Goal,
    Hypothesis,
    Observation,
    Relation,
    WorldEvent,
    WorldSnapshot,
    is_agent_actor,
)

_KIND_MODELS = {
    "entity": Entity,
    "relation": Relation,
    "observation": Observation,
    "artifact": Artifact,
    "capability": Capability,
    "hypothesis": Hypothesis,
    "goal": Goal,
    "action": ActionRecord,
    "constraint": Constraint,
    "failure": Failure,
}


class WorldReducer:
    def apply(self, snapshot: WorldSnapshot, event: WorldEvent) -> WorldSnapshot:
        collection = getattr(snapshot, KIND_COLLECTIONS[event.kind])
        model_type = _KIND_MODELS[event.kind]
        object_id = event.object.get("id")
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("world events require object.id")

        if event.op == "remove":
            collection.pop(object_id, None)
        else:
            payload = dict(event.object)
            provenance = dict(payload.get("provenance") or {})
            provenance["actor"] = event.actor
            provenance["event_id"] = event.id
            if event.source_event_id is not None:
                provenance["source_event_id"] = event.source_event_id
            declared_status = provenance.get("epistemic_status")
            if is_agent_actor(event.actor):
                provenance["epistemic_status"] = "claim"
            elif event.actor.startswith("benchmark:") or event.actor in {"grader", "verifier"}:
                provenance["epistemic_status"] = "evidence"
            elif event.actor == "harness" and declared_status in {"evidence", "verified"}:
                provenance["epistemic_status"] = declared_status
            else:
                provenance["epistemic_status"] = "unclassified"
            payload["provenance"] = provenance
            collection[object_id] = model_type.model_validate(payload)

        snapshot.revision += 1
        return snapshot

    def replay(self, events: list[WorldEvent]) -> WorldSnapshot:
        snapshot = WorldSnapshot()
        for event in events:
            self.apply(snapshot, event)
        return snapshot
