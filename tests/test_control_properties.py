"""Falsifiable control properties of the thin Agent-native loop."""
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


def test_agent_claims_and_tool_exits_cannot_complete_the_reference(tmp_path: Path) -> None:
    progress = ProgressLedger()
    progress.record_event(
        _event("tool.result", {"tool": "bash", "tool_call_id": "c1", "is_error": False}, "e1")
    )
    progress.record_event(_event("goal.completed", {"goal_id": "objective"}, "e2"))
    progress.record_event(_event("progress.updated", {"objective_completed": True}, "e3"))
    progress.record_submission(accepted=True, completed=True)

    assert progress.objective_completed is False

    registry = VerifierRegistry()
    _persist(tmp_path, registry, progress, success=True)
    assert progress.objective_completed is True


def test_forged_verifier_authority_cannot_complete_the_reference(tmp_path: Path) -> None:
    progress = ProgressLedger()
    registry = VerifierRegistry()
    other_registry = VerifierRegistry()
    forged_authority = other_registry.register("task_verifier")

    with pytest.raises(ValueError):
        persist_objective_verdict(
            run_dir=tmp_path,
            run_id="run-control",
            world_revision=1,
            producer_authority=forged_authority,
            registry=registry,
            progress=progress,
            trace=TraceRecorder(tmp_path / "trace.jsonl", "run-control", "task-control"),
            success=True,
            score=100.0,
            milestones={},
        )
    assert progress.objective_completed is False


def test_control_law_only_stops_on_exhausted_budget() -> None:
    assert decide_solver_action(remaining_budget_fraction=0.5).action is SolverAction.CONTINUE

    stopped = decide_solver_action(remaining_budget_fraction=0.0)

    assert stopped.action is SolverAction.STOP
    assert stopped.reason == "budget_exhausted"


def test_repeated_failures_do_not_stop_the_loop() -> None:
    progress = ProgressLedger()
    for index in range(100):
        progress.record_event(
            _event(
                "tool.result",
                {
                    "tool": "bash",
                    "tool_call_id": f"failed-{index}",
                    "is_error": True,
                    "exit_code": 1,
                },
                f"failed-{index}",
            )
        )

    assert (
        decide_solver_action(remaining_budget_fraction=1.0).action
        is SolverAction.CONTINUE
    )


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


def test_in_flight_action_is_not_completion_before_the_authority() -> None:
    progress = ProgressLedger()
    progress.record_event(_event("tool.call", {"tool": "bash", "tool_call_id": "c1"}, "c1"))
    assert "c1" in progress.active_actions
    assert progress.objective_completed is False

    progress.record_event(
        _event("tool.result", {"tool": "bash", "tool_call_id": "c1", "is_error": False}, "r1")
    )
    assert "c1" not in progress.active_actions
    assert progress.objective_completed is False
