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
def test_a_steady_loop_only_ever_continues_or_verifies() -> None:
    actions = {
        decide_solver_action(event_type=event_type).action
        for event_type in ("tool.result", "tool.call", "progress.updated", "world.observe")
    }

    assert actions == {SolverAction.CONTINUE, SolverAction.VERIFY}


def test_no_progress_converges_monotonically_to_stop() -> None:
    actions = [
        decide_solver_action(event_type="tool.result", no_progress_count=count).action
        for count in range(8)
    ]

    stop_index = actions.index(SolverAction.STOP)
    assert stop_index == 6  # DEFAULT_MAX_NO_PROGRESS
    assert all(action is SolverAction.STOP for action in actions[stop_index:])
    # No oscillation: before the bound the loop keeps verifying rather than
    # drifting into an unbounded middle state.
    assert all(action is SolverAction.VERIFY for action in actions[:stop_index])


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


def _record_tool(
    progress: ProgressLedger,
    *,
    call_id: str,
    family: str,
    fingerprint: str,
    failed: bool = False,
    exit_code: int = 0,
) -> None:
    progress.record_event(
        _event(
            "tool.call",
            {
                "tool": "bash",
                "tool_call_id": call_id,
                "args_family": family,
                "args_fingerprint": fingerprint,
            },
            f"call-{call_id}",
        )
    )
    progress.record_event(
        _event(
            "tool.result",
            {
                "tool": "bash",
                "tool_call_id": call_id,
                "is_error": failed,
                "exit_code": exit_code,
            },
            f"result-{call_id}",
        )
    )


def test_successful_different_commands_in_one_family_do_not_trigger_repetition() -> None:
    progress = ProgressLedger()

    for index in range(6):
        _record_tool(
            progress,
            call_id=f"success-{index}",
            family="bash:curl",
            fingerprint=f"fingerprint-{index}",
        )

    assert progress.repetition_action() is None
    assert progress.steered_tool_families == {}


def test_repeated_failures_in_one_family_escalate_to_convergence() -> None:
    progress = ProgressLedger()

    for index in range(3):
        _record_tool(
            progress,
            call_id=f"failure-{index}",
            family="bash:curl",
            fingerprint=f"fingerprint-{index}",
            failed=True,
        )

    action = progress.repetition_action()
    assert action is not None
    family, consecutive, level, basis, _fingerprint = action
    assert family == "bash:curl"
    assert consecutive == 3
    assert level == 1
    assert basis == "failure_family"

    progress.note_repetition_steer(family, level=1)
    for index in range(3, 6):
        _record_tool(
            progress,
            call_id=f"failure-{index}",
            family="bash:curl",
            fingerprint=f"fingerprint-{index}",
            failed=True,
        )

    escalation = progress.repetition_action()
    assert escalation is not None
    assert escalation[1] == 6
    assert escalation[2] == 2


def test_failures_spread_across_families_do_not_trigger_repetition() -> None:
    progress = ProgressLedger()

    for index, family in enumerate(("bash:curl", "bash:nmap", "bash:curl", "bash:nmap")):
        _record_tool(
            progress,
            call_id=f"rotate-{index}",
            family=family,
            fingerprint=f"rotate-fingerprint-{index}",
            failed=True,
        )

    assert progress.repetition_action() is None


def test_identical_arguments_trigger_repetition_even_when_execution_succeeds() -> None:
    progress = ProgressLedger()

    for index in range(3):
        _record_tool(
            progress,
            call_id=f"same-{index}",
            family="bash:curl",
            fingerprint="same-fingerprint",
        )

    action = progress.repetition_action()
    assert action is not None
    assert action[2] == 1
    assert action[3] == "identical_args"


def test_nonzero_exit_code_counts_as_a_repetition_failure() -> None:
    progress = ProgressLedger()

    for index in range(3):
        _record_tool(
            progress,
            call_id=f"exit-{index}",
            family="bash:ffuf",
            fingerprint=f"exit-fingerprint-{index}",
            exit_code=1,
        )

    action = progress.repetition_action()
    assert action is not None
    assert action[0] == "bash:ffuf"
    assert action[3] == "failure_family"


def test_trusted_progress_resets_repetition_but_rejection_does_not() -> None:
    progress = ProgressLedger()
    for index in range(3):
        _record_tool(
            progress,
            call_id=f"reset-{index}",
            family="bash:curl",
            fingerprint=f"reset-fingerprint-{index}",
            failed=True,
        )

    progress.record_submission(accepted=False, completed=False)
    assert progress.repetition_action() is not None

    progress.record_submission(accepted=True, completed=False)
    assert progress.repetition_action() is None
    assert progress.tool_family_failures == {}


def test_old_progress_payload_without_new_repetition_fields_still_loads() -> None:
    payload = ProgressLedger().model_dump(mode="json")
    for field in (
        "tool_family_failures",
        "last_failed_tool_family",
        "tool_fingerprint_streak",
        "last_tool_fingerprint",
    ):
        payload.pop(field, None)

    restored = ProgressLedger.model_validate(payload)

    assert restored.tool_family_failures == {}
    assert restored.tool_fingerprint_streak == 0


# 5. Counterfactual decision: a change in estimated state changes the action.
def test_remaining_budget_alters_the_next_decision() -> None:
    funded = decide_solver_action(event_type="tool.call")
    starved = decide_solver_action(
        event_type="tool.call", no_progress_count=1, remaining_budget_fraction=0.04
    )

    assert funded.action is SolverAction.CONTINUE
    assert starved.action is SolverAction.STOP
    assert starved.reason == "low_budget_without_progress"


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
