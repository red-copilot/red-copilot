"""Falsifiable control properties of the single closed-loop control spine.

These tests encode the cybernetic invariants the simplified architecture relies
on: only the authoritative sensor can close the loop, the loop is bounded and
converges to a stop instead of oscillating, the reference is immutable, only
observable signals count as feedback, and in-flight (dead-time) actions are not
mistaken for completion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.progress import ProgressLedger
from harness.runtime.evidence import persist_objective_verdict
from harness.session import AgentEvent
from harness.solver_policy import SolverAction, decide_solver_action
from harness.trace import TraceRecorder
from harness.verification import VerifierRegistry


def _event(kind: str, data: dict, event_id: str) -> AgentEvent:
    return AgentEvent(type=kind, data=data, event_id=event_id)


def _persist(
    tmp_path: Path, registry: VerifierRegistry, progress: ProgressLedger, *, success: bool
) -> None:
    authority = registry.register("task_verifier")
    persist_objective_verdict(
        run_dir=tmp_path,
        run_id="run-control",
        world_revision=3,
        producer_authority=authority,
        registry=registry,
        progress=progress,
        trace=TraceRecorder(tmp_path / "trace.jsonl", "run-control", "task-control"),
        success=success,
        score=100.0 if success else 0.0,
        milestones={"objective": success},
    )


# 1. Feedback authority: only the authoritative sensor closes the loop.
def test_agent_claims_and_tool_exits_cannot_complete_the_reference(tmp_path: Path) -> None:
    progress = ProgressLedger()
    progress.record_event(
        _event("tool.result", {"tool": "bash", "tool_call_id": "c1", "is_error": False}, "e1")
    )
    progress.record_event(_event("goal.completed", {"goal_id": "objective"}, "e2"))
    progress.record_event(_event("progress.updated", {"objective_completed": True}, "e3"))

    assert progress.objective_completed is False
    assert progress.completion_claims  # recorded, but only as claims

    registry = VerifierRegistry()
    _persist(tmp_path, registry, progress, success=True)
    assert progress.objective_completed is True


def test_forged_verifier_authority_cannot_complete_the_reference(tmp_path: Path) -> None:
    progress = ProgressLedger()
    registry = VerifierRegistry()
    other = VerifierRegistry()
    forged = other.register("task_verifier")

    with pytest.raises(ValueError):
        persist_objective_verdict(
            run_dir=tmp_path,
            run_id="run-control",
            world_revision=1,
            producer_authority=forged,
            registry=registry,
            progress=progress,
            trace=TraceRecorder(tmp_path / "trace.jsonl", "run-control", "task-control"),
            success=True,
            score=100.0,
            milestones={},
        )
    assert progress.objective_completed is False


# 2. Stability: the loop is bounded and converges to STOP instead of oscillating.
def test_replans_are_capped_and_converge_to_stop() -> None:
    actions: list[SolverAction] = []
    replans_used = 0
    while True:
        decision = decide_solver_action(
            event_type="world.observe",
            replan_reasons=["world_state_changed"],
            no_progress_count=1,
            replans_used=replans_used,
        )
        actions.append(decision.action)
        if decision.action is SolverAction.REPLAN:
            replans_used += 1
            continue
        assert decision.action is SolverAction.STOP
        assert decision.reason == "replan_limit"
        break

    assert replans_used == 8
    assert actions[-1] is SolverAction.STOP
    assert all(action is SolverAction.REPLAN for action in actions[:-1])


def test_no_progress_converges_monotonically_to_stop() -> None:
    actions = [
        decide_solver_action(event_type="tool.result", no_progress_count=count).action
        for count in range(8)
    ]

    stop_index = actions.index(SolverAction.STOP)
    assert stop_index == 6  # DEFAULT_MAX_NO_PROGRESS
    assert all(action is SolverAction.STOP for action in actions[stop_index:])
    # No oscillation: once the loop starts replanning it does not fall back to VERIFY.
    replan_index = actions.index(SolverAction.REPLAN)
    assert SolverAction.VERIFY not in actions[replan_index:]


def test_low_budget_without_progress_stops_before_budget_is_spent() -> None:
    decision = decide_solver_action(
        event_type="tool.call",
        no_progress_count=2,
        remaining_budget_fraction=0.04,
    )
    assert decision.action is SolverAction.STOP
    assert decision.reason == "low_budget_without_progress"


# 3. Reference integrity: agent output cannot rewrite the objective/setpoint.
def test_agent_events_cannot_rewrite_the_reference() -> None:
    progress = ProgressLedger(active_goal="objective-from-harness")
    for index in range(25):
        progress.record_event(
            _event("goal.completed", {"goal_id": "attacker-goal"}, f"g{index}")
        )
        progress.record_event(
            _event(
                "progress.updated",
                {"objective_completed": True, "goal_id": "attacker-goal"},
                f"p{index}",
            )
        )

    assert progress.active_goal == "objective-from-harness"
    assert progress.objective_completed is False


# 4. Observability: unreferenced signals are claims, not feedback.
def test_unreferenced_signals_stay_claims_and_tool_exits_are_not_progress() -> None:
    progress = ProgressLedger()
    progress.record_event(
        _event("world.observe", {"type": "entity", "summary": "host up"}, "w1")
    )
    progress.record_event(
        _event("progress.updated", {"confirmed_fact": "the flag is flag{x}"}, "p1")
    )

    assert progress.confirmed_facts == []
    assert "the flag is flag{x}" in progress.claims

    progress.record_event(
        _event("tool.result", {"tool": "bash", "tool_call_id": "c1", "is_error": False}, "t1")
    )
    assert progress.execution_succeeded == 1
    assert progress.no_progress_count == 0
    assert progress.unconfirmed_progress == 1
    assert progress.evidence_confirmed == 0
    assert progress.objective_completed is False


# 2b. A successful execution is neutral: it must not trigger the hard stall stop.
def test_successful_executions_do_not_stall_stop_but_failures_do() -> None:
    progress = ProgressLedger()
    for index in range(6):
        progress.record_event(
            _event(
                "tool.result",
                {"tool": "bash", "tool_call_id": f"ok-{index}", "is_error": False},
                f"ok-{index}",
            )
        )
    decision = decide_solver_action(
        event_type="tool.result",
        no_progress_count=progress.no_progress_count,
        unconfirmed_progress=progress.unconfirmed_progress,
    )
    assert progress.no_progress_count == 0
    assert progress.unconfirmed_progress == 6
    assert decision.action is not SolverAction.STOP

    stalled = ProgressLedger()
    for index in range(6):
        stalled.record_event(
            _event(
                "tool.result",
                {"tool": "bash", "tool_call_id": f"bad-{index}", "is_error": True},
                f"bad-{index}",
            )
        )
    stalled_decision = decide_solver_action(
        event_type="tool.result",
        no_progress_count=stalled.no_progress_count,
        unconfirmed_progress=stalled.unconfirmed_progress,
    )
    assert stalled.no_progress_count == 6
    assert stalled_decision.action is SolverAction.STOP
    assert stalled_decision.reason == "no_progress_limit"


def test_unconfirmed_progress_bound_terminates_an_unproductive_loop() -> None:
    decision = decide_solver_action(
        event_type="tool.result",
        no_progress_count=0,
        unconfirmed_progress=40,
    )
    assert decision.action is SolverAction.STOP
    assert decision.reason == "unconfirmed_progress_limit"


# 5. Counterfactual decision: a change in estimated state changes the action.
def test_world_state_change_alters_the_next_decision() -> None:
    steady = decide_solver_action(event_type="world.observe")
    changed = decide_solver_action(
        event_type="world.observe", replan_reasons=["world_state_changed"]
    )

    assert steady.action is SolverAction.VERIFY
    assert changed.action is SolverAction.REPLAN
    assert changed.reason == "world_state_changed"


# 6. Dead time: a dispatched action is not completion until it is verified.
def test_in_flight_action_is_not_completion_before_a_verdict() -> None:
    progress = ProgressLedger()
    progress.record_event(_event("tool.call", {"tool": "bash", "tool_call_id": "c1"}, "c1"))
    assert "c1" in progress.active_actions
    assert progress.objective_completed is False

    progress.record_event(
        _event("tool.result", {"tool": "bash", "tool_call_id": "c1", "is_error": False}, "r1")
    )
    assert "c1" not in progress.active_actions
    assert progress.objective_completed is False
