from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness.aci import ActionIntent, TypedACI
from harness.agent_events import TYPED_EVENT_SCHEMAS, validate_typed_event
from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.world import SQLiteWorldRepository


def test_registry_covers_exactly_the_agent_authored_event_types() -> None:
    assert set(TYPED_EVENT_SCHEMAS) == {
        "action.intent",
        "world.observe",
        "world.hypothesis",
        "world.capability",
        "world.artifact",
        "world.failure",
    }


def test_registry_covers_exactly_the_events_typed_aci_validates(tmp_path) -> None:
    """TypedACI and the schema registry must not drift apart."""
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger()
    aci = TypedACI()

    handled = {
        event_type
        for event_type in TYPED_EVENT_SCHEMAS
        if aci.apply(
            AgentEvent(
                type=event_type,
                data={"type": "x", "statement": "s", "message": "m"},
            ),
            world=world,
            progress=progress,
            actor="agent:test",
        )
        >= 0
    }
    assert handled == set(TYPED_EVENT_SCHEMAS)


def test_valid_payloads_pass_and_unknown_keys_are_ignored() -> None:
    payload = validate_typed_event(
        "world.observe",
        {
            "type": "web.endpoint",
            "content": {"path": "/admin"},
            "confidence": 0.9,
            "future_field": {"ignored": True},
        },
    )
    assert payload.type == "web.endpoint"
    assert payload.confidence == 0.9
    assert not hasattr(payload, "future_field")


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("world.observe", {"id": "obs-1"}),  # missing required type
        ("world.observe", {"type": ""}),  # empty type
        ("world.observe", {"type": "x", "confidence": 1.5}),  # out of range
        ("world.observe", {"type": "x", "content": ["not", "a", "dict"]}),
        ("world.hypothesis", {"confidence": 0.5}),  # missing statement
        ("world.hypothesis", {"statement": "s", "status": "bogus"}),
        ("world.capability", {"subject": "s"}),  # missing type
        ("world.artifact", {"uri": "file:///tmp"}),  # missing type
        ("world.failure", {"type": "tool.error"}),  # missing message
        ("world.failure", {"type": "t", "message": ""}),  # empty message
    ],
)
def test_malformed_payloads_fail_closed(event_type: str, data: dict) -> None:
    with pytest.raises(ValidationError):
        validate_typed_event(event_type, data)


def test_malformed_payloads_reject_in_typed_aci_without_state_changes(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger()
    aci = TypedACI()

    with pytest.raises(ValueError):
        aci.apply(
            AgentEvent(type="world.hypothesis", data={"confidence": 0.5}),
            world=world,
            progress=progress,
            actor="agent:test",
        )
    assert world.snapshot.revision == 0


def test_action_intent_schema_generates_ids_and_keeps_import_path() -> None:
    payload = ActionIntent.model_validate({"description": "probe"})
    assert payload.action_id.startswith("action_")
    assert payload.schema_version == "harness/action-intent/v1"

    from harness.aci import ActionIntent as aci_action_intent

    assert aci_action_intent is ActionIntent


def test_numeric_confidence_string_still_coerces_like_before() -> None:
    payload = validate_typed_event(
        "world.hypothesis", {"statement": "s", "confidence": "0.5"}
    )
    assert payload.confidence == 0.5
