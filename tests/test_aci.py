from harness.aci import TypedACI
from harness.progress import ProgressLedger
from harness.session import AgentEvent
from harness.world import SQLiteWorldRepository
from harness.world.live import WorldInboxCursor


def test_typed_aci_turns_semantic_events_into_world_state(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger()
    aci = TypedACI()

    aci.apply(
        AgentEvent(
            type="action.intent",
            data={
                "action_id": "action-1",
                "description": "probe admin endpoint",
                "expected_observations": ["admin endpoint exists"],
            },
        ),
        world=world,
        progress=progress,
        actor="agent:test",
    )
    mutations = aci.apply(
        AgentEvent(
            type="world.observe",
            data={
                "id": "obs-admin",
                "type": "web.endpoint",
                "summary": "admin endpoint exists at /admin",
                "content": {"path": "/admin", "status": 403},
                "confidence": 1.0,
            },
        ),
        world=world,
        progress=progress,
        actor="agent:test",
    )

    assert mutations == 1
    assert world.snapshot.observations["obs-admin"].content["status"] == 403
    assert world.snapshot.observations["obs-admin"].provenance.actor == "agent:test"
    assert world.snapshot.observations["obs-admin"].provenance.epistemic_status == "claim"
    assert progress.expected_observation == "admin endpoint exists"
    assert progress.actual_observation == "admin endpoint exists at /admin"


def test_typed_aci_automatically_records_tool_execution(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    progress = ProgressLedger()
    aci = TypedACI()

    assert (
        aci.apply(
            AgentEvent(
                type="tool.result",
                data={
                    "tool": "bash",
                    "tool_call_id": "call-1",
                    "is_error": False,
                    "duration_ms": 10,
                },
            ),
            world=world,
            progress=progress,
            actor="agent:test",
        )
        == 2
    )

    assert world.snapshot.actions["action:call-1"].status == "succeeded"
    assert world.snapshot.observations["obs:tool:call-1"].type == "tool.execution"
    assert world.snapshot.observations["obs:tool:call-1"].provenance.epistemic_status == "evidence"


def test_untrusted_world_inbox_cannot_upgrade_a_claim_to_verified(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.db")
    inbox_path = tmp_path / "world.inbox.jsonl"
    inbox_path.write_text(
        '{"kind":"observation","object":{"id":"obs-1","type":"web.endpoint",'
        '"provenance":{"epistemic_status":"verified"}}}\n',
        encoding="utf-8",
    )

    report = WorldInboxCursor(inbox_path, world, actor="agent:test").poll()

    assert report.accepted == 1
    assert world.snapshot.observations["obs-1"].provenance.epistemic_status == "claim"


def test_tool_output_is_extracted_into_evidence_observations(tmp_path) -> None:
    world = SQLiteWorldRepository(tmp_path / "world.events.jsonl")
    progress = ProgressLedger()
    output = (
        "Starting Nmap\n"
        "80/tcp   open  http    Apache httpd 2.4.41\n"
        "443/tcp  open  ssl/https\n"
        "\n"
        "http://10.0.0.5/admin  (Status: 200) [Size: 1234]\n"
        "\n"
        "login   [Status: 302, Size: 0, Words: 1, Lines: 1]\n"
        "static  [Status: 200, Size: 42, Words: 1, Lines: 1]\n"
    )
    typed = TypedACI()
    typed.apply(
        AgentEvent(
            type="tool.result",
            data={
                "tool": "bash",
                "tool_call_id": "c1",
                "output_excerpt": output,
                "is_error": False,
            },
        ),
        world=world,
        progress=progress,
        actor="agent:pi",
    )

    snapshot = world.snapshot
    services = {o.id: o for o in snapshot.observations.values() if o.type == "network.service"}
    endpoints = {o.id: o for o in snapshot.observations.values() if o.type == "web.endpoint"}
    assert {o.content["port"] for o in services.values()} == {80, 443}
    assert all(o.provenance.epistemic_status == "evidence" for o in services.values())
    assert len(endpoints) == 3
    assert {o.content.get("status") for o in endpoints.values()} == {200, 302}

    # Repeated identical output is content-addressed, so it does not duplicate.
    typed.apply(
        AgentEvent(
            type="tool.result",
            data={
                "tool": "bash",
                "tool_call_id": "c2",
                "output_excerpt": output,
                "is_error": False,
            },
        ),
        world=world,
        progress=progress,
        actor="agent:pi",
    )
    assert len([o for o in world.snapshot.observations.values() if o.type == "web.endpoint"]) == 3
