"""Falsifiable control properties of the single closed-loop control spine.

These tests encode the cybernetic invariants the simplified architecture relies
on: only the authoritative sensor can close the loop, the Harness stops only on
an external bound, the reference is immutable, unreferenced signals are claims,
and in-flight actions are not mistaken for completion.
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


# 2. Stability: without an external bound the loop never invents a stop.
def test_a_steady_loop_only_continues() -> None:
    actions = {
        decide_solver_action(remaining_budget_fraction=fraction).action
        for fraction in (1.0, 0.5, 0.05, 0.0001)
    }

    assert actions == {SolverAction.CONTINUE}


def test_only_zero_remaining_budget_stops_the_loop() -> None:
    decisions = [
        decide_solver_action(remaining_budget_fraction=fraction)
        for fraction in (1.0, 0.5, 0.05, 0.0001, 0.0)
    ]

    assert all(decision.action is SolverAction.CONTINUE for decision in decisions[:-1])
    assert decisions[-1].action is SolverAction.STOP
    assert decisions[-1].reason == "budget_exhausted"


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


def test_internal_stall_counters_do_not_decide_whether_to_stop() -> None:
    """Counters stay available for metrics, but they are not a control input."""
    progress = ProgressLedger(no_progress_count=10_000, unconfirmed_progress=10_000)

    decision = decide_solver_action(
        remaining_budget_fraction=progress.remaining_budget_fraction
    )

    assert decision.action is SolverAction.CONTINUE


# 5. Counterfactual decision: only a change in the external budget changes it.
def test_remaining_budget_alters_the_next_decision() -> None:
    funded = decide_solver_action(remaining_budget_fraction=0.5)
    starved = decide_solver_action(remaining_budget_fraction=0.0)

    assert funded.action is SolverAction.CONTINUE
    assert starved.action is SolverAction.STOP
    assert starved.reason == "budget_exhausted"


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
