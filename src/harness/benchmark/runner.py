# ruff: noqa: I001
from __future__ import annotations

import hashlib
import asyncio
import contextlib
import math
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..agent import AgentResult, AgentRunContext, build_agent_adapter
from ..models import AgentSpec, BudgetSpec, TaskSpec
from ..session import AgentObservation
from ..solver_loop import missing_summary_sections
from ..trace import TraceRecorder, sanitize_observability_data
from ..verification import ObservationRecord
from ..budget import FINALIZE_GRACE_SECONDS, UsageMetrics, enforced_budget_limits
from ..progress import SLOW_TOOL_CALLS, ProgressLedger, empty_steer_counts
from ..runtime import SolverRuntime, bootstrap_solver
from ..runtime.executors import (
    ExecutorSessionPlan,
    FindingsArchive,
    fold_session_progress,
    plan_executor_sessions,
    prepare_executor_directories,
    prepare_shared_findings,
)
from ..agent_workspace import seed_notes_from_attempt, sync_agent_workspace
from ..runtime.checkpoint import run_action_owner, save_session_checkpoint
from ..runtime.evidence import persist_objective_verdict
from ..runtime.lifecycle import RunLifecycle, resolve_run_status
from ..runtime.projections import finalize_world_goal, network_result, run_summary, world_result
from ..runtime.results import persist_run_result, submission_fraction
from ..world import (
    Entity,
    Failure,
    Goal,
    Observation,
    SQLiteWorldRepository,
    WorldRepository,
)
from .base import BenchmarkAdapter, BenchmarkCase, BenchmarkSession, EvaluationResult, Submission
from .protocol import SubmissionInbox


SubmissionExtractor = Callable[[AgentResult], list[Submission]]
BENCHMARK_TEARDOWN_TIMEOUT_SECONDS = 30.0
# Externally evaluated benchmarks score only through submitted candidates, so a
# run that has burned half its wall clock without a single submission is steered
# once to consolidate what it holds or change attack surface instead of grinding
# the same path to the deadline.
BENCHMARK_SUBMISSION_DUE_FRACTION = 0.5


async def _evaluate_benchmark(
    adapter: BenchmarkAdapter,
    session: BenchmarkSession,
    *,
    timeout: float,
) -> EvaluationResult:
    """Apply the shared evaluation contract and fail closed on timeout or bad output."""
    try:
        raw = await asyncio.wait_for(adapter.evaluate(session), timeout=max(timeout, 0.001))
    except TimeoutError as exc:
        raise RuntimeError("benchmark evaluator timed out") from exc
    if isinstance(raw, EvaluationResult):
        result = raw
    else:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("success"), bool)
            or isinstance(raw.get("score", 0), bool)
            or not isinstance(raw.get("score", 0), (int, float))
        ):
            raise TypeError("benchmark evaluator returned an inconsistent result")
        try:
            result = EvaluationResult.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise TypeError("benchmark evaluator returned an inconsistent result") from exc
    if not math.isfinite(result.score) or result.score < 0:
        raise ValueError("benchmark evaluator returned an inconsistent score")
    return result


def _submission_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _ExecutorRuntime:
    """Per-session control state for one competing executor."""

    plan: ExecutorSessionPlan
    task: TaskSpec
    runtime: SolverRuntime
    progress_path: Path
    submission_inbox: SubmissionInbox
    session: Any = None
    result: AgentResult | None = None
    status: str = "pending"
    completed: bool = False
    submissions: int = 0
    first_submission_at: float | None = None
    summary_missing_sections: list[str] = field(default_factory=list)
    retired_results: list[AgentResult] = field(default_factory=list)
    restarts: int = 0
    restart_briefs: list[dict[str, Any]] = field(default_factory=list)


def _summary_key(key: tuple[str | None, str | None, str | None]) -> tuple[str, str, str]:
    prompt, provider, model = key
    return (
        "" if prompt is None else prompt,
        "" if provider is None else provider,
        "" if model is None else model,
    )


def _first_submission_ms(
    runtimes: Sequence[_ExecutorRuntime], started: float
) -> int | None:
    """Milliseconds from run start to the first submitted candidate, or ``None``."""
    stamps = [
        item.first_submission_at
        for item in runtimes
        if item.first_submission_at is not None
    ]
    if not stamps:
        return None
    return max(0, int((min(stamps) - started) * 1000))



def _merged_loop_metrics(runtimes: Sequence[_ExecutorRuntime]) -> dict[str, Any]:
    """Fold per-session solver metrics into the run-level metric block.

    Competing sessions each own a loop; the run reports the sum of their tool
    time and steer volume, the largest final context, and the slowest calls
    across sessions. This is the vocabulary `harness report` and the sweep
    summary read, so it stays flat and bounded.
    """
    steer_counts = empty_steer_counts()
    tool_calls = tool_failures = tool_time_ms = context_tokens_final = 0
    slow: list[dict[str, Any]] = []
    for item in runtimes:
        snapshot = item.runtime.loop.metrics_snapshot()
        for bucket, count in snapshot["steer_counts"].items():
            if bucket in steer_counts:
                steer_counts[bucket] += count
        tool_calls += int(snapshot["tool_calls"])
        tool_failures += int(snapshot["tool_failures"])
        tool_time_ms += int(snapshot["tool_time_ms"])
        context_tokens_final = max(context_tokens_final, int(snapshot["context_tokens_final"]))
        slow.extend(snapshot["slow_tool_calls"])
    slow.sort(key=lambda entry: entry["duration_ms"], reverse=True)
    return {
        "steer_counts": steer_counts,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "tool_time_ms": tool_time_ms,
        "slow_tool_calls": slow[:SLOW_TOOL_CALLS],
        "context_tokens_final": context_tokens_final,
    }


def _executor_summary(runtimes: Sequence[_ExecutorRuntime]) -> list[dict[str, Any]]:
    """Aggregate competing sessions by prompt, provider and model.

    This is the post-run counterpart of launching several sessions: it answers
    "which prompt and model actually solved things" without re-reading the
    per-session artifacts by hand. Only competing runs emit it.
    """
    grouped: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    for item in runtimes:
        pi = item.plan.agent.pi
        key = (
            pi.system_prompt_file if pi else None,
            pi.provider if pi else None,
            pi.model if pi else None,
        )
        entry = grouped.setdefault(
            key,
            {
                "prompt": key[0],
                "provider": key[1],
                "model": key[2],
                "sessions": 0,
                "submissions": 0,
                "completed_sessions": 0,
                "budget_used": {},
                "total_tokens": 0,
            },
        )
        entry["sessions"] += 1
        entry["submissions"] += item.submissions
        entry["completed_sessions"] += 1 if item.completed else 0
        for dimension, value in item.runtime.progress.budget_used.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry["budget_used"][dimension] = (
                    entry["budget_used"].get(dimension, 0) + value
                )
        metrics = item.result.metrics.as_dict() if item.result else {}
        tokens = metrics.get("total_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            entry["total_tokens"] += tokens
    return [grouped[key] for key in sorted(grouped, key=_summary_key)]


async def _session_identity(session: Any) -> str | None:
    """Best-effort Agent session id for the restart trace."""
    get_checkpoint = getattr(session, "checkpoint", None)
    if not callable(get_checkpoint):
        return None
    try:
        cursor = await get_checkpoint()
    except Exception:  # noqa: BLE001 - identity is telemetry, not control
        return None
    identity = getattr(cursor, "session_id", None)
    return identity if isinstance(identity, str) and identity else None


def _archive_session_transcript(*, workspace: Path, attempt: int) -> None:
    """Keep the retired session's raw transcript next to the new one."""
    for name, archived in (
        ("agent.stdout.log", f"agent.stdout.restart-{attempt}.log"),
        ("events.jsonl", f"events.restart-{attempt}.jsonl"),
    ):
        source = workspace / name
        if not source.is_file():
            continue
        shutil.copyfile(source, workspace / archived)


_CONTEXT_BRIEF_SECTIONS = ("Established facts", "Ruled out", "Open intents")


def _empty_brief_sections(brief: str) -> list[str]:
    """Report which carry-forward sections had nothing recorded."""
    lines = brief.splitlines()
    empty: list[str] = []
    for index, line in enumerate(lines):
        heading = line.strip().removeprefix("## ").strip()
        if heading not in _CONTEXT_BRIEF_SECTIONS:
            continue
        body = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if body == "- none recorded":
            empty.append(heading)
    return empty


def _notes_file_count(workspace: Path) -> int:
    """Count the Agent's note files at the moment of a restart."""
    notes_dir = workspace / "notes"
    if not notes_dir.is_dir():
        return 0
    return sum(1 for path in notes_dir.rglob("*") if path.is_file())


async def _restart_session(
    *,
    executor: _ExecutorRuntime,
    run_context: AgentRunContext,
    adapter_instance: Any,
    reason: str,
    trace: TraceRecorder,
) -> bool:
    """Reset the Agent session and hand it the carry-forward brief.

    The workspace, notes and container mounts are untouched: only the model's
    transcript is discarded. Returns ``False`` when the restart could not be
    completed, which tells the caller to end the session instead of looping.
    """
    loop = executor.runtime.loop
    progress = executor.runtime.progress
    _, brief = loop.write_carry_forward_brief(reason)
    brief_metrics = {
        "brief_chars": len(brief),
        "notes_count": _notes_file_count(executor.plan.workspace),
        "empty_sections": _empty_brief_sections(brief),
    }
    previous = executor.session
    from_session = await _session_identity(previous)
    attempt = executor.restarts + 1
    degraded: dict[str, Any] = {
        "reason": reason,
        "from_session": from_session,
        "to_session": None,
        "degraded": True,
        **brief_metrics,
    }
    try:
        await previous.close(f"context-restart:{reason}")
        retired = await previous.result()
    except Exception as exc:  # noqa: BLE001 - keep the run, lose only the reset
        trace.emit(
            "solver.context_restart",
            actor="harness",
            data={**degraded, "error_type": type(exc).__name__, "stage": "close"},
        )
        return False
    executor.retired_results.append(retired)
    progress.record_context_restart()
    _archive_session_transcript(workspace=executor.plan.workspace, attempt=attempt)
    try:
        executor.session = await adapter_instance.start_session(run_context)
    except Exception as exc:  # noqa: BLE001
        trace.emit(
            "solver.context_restart",
            actor="harness",
            data={**degraded, "error_type": type(exc).__name__, "stage": "start"},
        )
        return False
    executor.restarts = attempt
    executor.restart_briefs.append(
        {"index": attempt, "reason": reason, **brief_metrics}
    )
    trace.emit(
        "solver.context_restart",
        actor="harness",
        data={
            "reason": reason,
            "from_session": from_session,
            "to_session": await _session_identity(executor.session),
            "degraded": False,
            "restart": attempt,
            **brief_metrics,
        },
    )
    # The brief rides in as the first observation, so it reaches the resumed
    # process together with the objective instead of mid-turn.
    await executor.session.observe(
        AgentObservation(
            type="solver.context_restart",
            data={
                "reason": reason,
                "brief_file": "carry-forward.md",
                "brief_chars": len(brief),
                "message": brief,
            },
        )
    )
    return True


def _merge_results(results: Sequence[AgentResult]) -> AgentResult:
    """Fold competing session results into one run-level result.

    Usage is summed; the first session-specific stop reason names the run's
    exhaustion. Raw stdout/stderr stay in the per-session artifacts. Session
    restarts reuse it so the retired transcripts stay in the accounting.
    """
    totals: dict[str, int | float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "cost_usd": 0.0,
    }
    for result in results:
        for key, value in result.metrics.as_dict().items():
            totals[key] = totals.get(key, 0) + value
    return AgentResult(
        returncode=0 if all(result.returncode == 0 for result in results) else 1,
        timed_out=any(result.timed_out for result in results),
        budget_exceeded=next(
            (result.budget_exceeded for result in results if result.budget_exceeded),
            None,
        ),
        stdout="",
        stderr="",
        metrics=UsageMetrics(**totals),
    )


async def _race_sessions(
    runtimes: Sequence[_ExecutorRuntime],
    launch: Callable[[_ExecutorRuntime], Awaitable[None]],
    trace: TraceRecorder,
) -> None:
    """Run every session at once and stop the losers on the first objective win.

    A session that raises only takes itself out of the race: the other sessions
    keep working, and the run fails closed only when nothing is left to evaluate.
    """
    tasks = {
        asyncio.create_task(launch(item), name=f"executor:{item.plan.executor_id}"): item
        for item in runtimes
    }
    pending = set(tasks)
    failures: list[BaseException] = []
    won = False
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                item = tasks[task]
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one session must not end the race.
                    item.status = "error"
                    failures.append(exc)
                    trace.emit(
                        "executor.session.error",
                        actor="harness",
                        data={
                            "executor_id": item.plan.executor_id,
                            "error_type": type(exc).__name__,
                            "message": "competing session failed; details omitted",
                        },
                    )
                if item.completed:
                    won = True
            if won:
                break
        if won:
            for task in pending:
                tasks[task].status = "cancelled"
                task.cancel()
        for task in list(pending):
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception as exc:  # noqa: BLE001 - losers may fail while stopping.
                trace.emit(
                    "executor.session.stop_error",
                    actor="harness",
                    data={
                        "executor_id": tasks[task].plan.executor_id,
                        "error_type": type(exc).__name__,
                        "message": "session failed while stopping; details omitted",
                    },
                )
        if not won and failures and len(failures) == len(runtimes):
            raise failures[0]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BenchmarkRunner:
    """Generic lifecycle for externally provisioned and evaluated benchmarks."""

    def __init__(
        self,
        *,
        runs_root: str | Path,
    ) -> None:
        self.runs_root = Path(runs_root)

    async def _consume_session(
        self,
        *,
        executor: _ExecutorRuntime,
        planned: ExecutorSessionPlan,
        run_context: AgentRunContext,
        adapter_instance: Any,
        trace: TraceRecorder,
        world: WorldRepository,
        archive: FindingsArchive | None,
        submit_candidate: Callable[
            [Submission, _ExecutorRuntime | None], Awaitable[bool]
        ],
    ) -> bool:
        """Consume one Agent session stream.

        Returns ``True`` when the session was reset for a fresh context, which
        tells the caller to keep driving the (already replaced) session.
        """
        session = executor.session
        async for event in session.events():
            decision = await executor.runtime.loop.process_event(session, event)
            sync_agent_workspace(run_dir=planned.root, workspace=planned.workspace)
            await save_session_checkpoint(
                run_dir=planned.root,
                session=session,
                world_revision=world.snapshot.revision,
                progress=executor.runtime.progress,
                processed_event_ids=executor.runtime.loop.processed_event_ids,
            )
            for submission in executor.submission_inbox.poll():
                executor.submissions += 1
                if await submit_candidate(submission, executor):
                    executor.completed = True
                    await session.close("objective-complete")
                    break
            if archive is not None:
                archive.snapshot()
            if executor.completed:
                return False
            restart_reason = executor.runtime.progress.context_restart_due()
            if restart_reason is not None:
                restarted = await _restart_session(
                    executor=executor,
                    run_context=run_context,
                    adapter_instance=adapter_instance,
                    reason=restart_reason,
                    trace=trace,
                )
                if not restarted:
                    await executor.session.close("context-restart-failed")
                    return False
                await save_session_checkpoint(
                    run_dir=planned.root,
                    session=executor.session,
                    world_revision=world.snapshot.revision,
                    progress=executor.runtime.progress,
                    processed_event_ids=executor.runtime.loop.processed_event_ids,
                )
                return True
            if decision.action.value == "stop":
                await session.close(f"solver-stop:{decision.reason}")
                return False
        return False

    async def run_case(
        self,
        *,
        adapter: BenchmarkAdapter,
        case: BenchmarkCase,
        agent: AgentSpec,
        budgets: BudgetSpec,
        seed: int,
        submission_extractor: SubmissionExtractor,
        seed_notes_from: Path | None = None,
    ) -> dict:
        if agent.network == "environment":
            raise ValueError(
                "externally provisioned benchmarks do not provide a Harness environment network; "
                "configure the Pi container with network: host or a future benchmark network"
            )
        run_id = f"{case.benchmark}_{case.id}_{uuid.uuid4().hex[:8]}"
        run_dir = (self.runs_root / run_id).resolve()
        # Provision before anything touches the disk. A refused start (platform
        # outage, closed challenge, instance cap) must not leave an empty run
        # directory behind, and a setup failure after a successful start must
        # not leak the challenge instance.
        session = await adapter.provision(case)
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            plan = plan_executor_sessions(agent=agent, budgets=budgets, run_dir=run_dir)
            prepare_executor_directories(plan)
            if seed_notes_from is not None:
                for planned_session in plan.sessions:
                    seed_notes_from_attempt(
                        workspace=planned_session.workspace,
                        previous_run_dir=seed_notes_from,
                    )
            shared_findings = prepare_shared_findings(plan)
            archive = FindingsArchive(run_dir=run_dir) if plan.competing else None
            trace = TraceRecorder(run_dir / "trace.jsonl", run_id, case.id)
        except BaseException:
            # Bounded like the terminal teardown: a refused setup must not turn
            # into a hung sweep, and the setup error is the one that matters.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    adapter.teardown(session),
                    timeout=BENCHMARK_TEARDOWN_TIMEOUT_SECONDS,
                )
            raise
        lifecycle = RunLifecycle()
        lifecycle.transition("running")
        teardown_error: str | None = None
        teardown_started = False

        async def teardown_once() -> None:
            nonlocal teardown_error, teardown_started
            if teardown_started:
                return
            teardown_started = True
            cleanup_task = asyncio.create_task(adapter.teardown(session))
            interrupted = False
            cleanup_exception: BaseException | None = None
            deadline = (
                asyncio.get_running_loop().time() + BENCHMARK_TEARDOWN_TIMEOUT_SECONDS
            )
            try:
                while True:
                    remaining = max(
                        0.0, deadline - asyncio.get_running_loop().time()
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(cleanup_task), timeout=remaining
                        )
                        break
                    except asyncio.CancelledError as exc:
                        if cleanup_task.done():
                            if cleanup_task.cancelled():
                                cleanup_exception = exc
                            break
                        interrupted = True
                    except TimeoutError as exc:
                        cleanup_exception = exc
                        cleanup_task.cancel()
                        try:
                            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=1.0)
                        except (asyncio.CancelledError, TimeoutError):
                            pass
                        except Exception as cancel_exc:  # noqa: BLE001
                            cleanup_exception = cleanup_exception or cancel_exc
                        break
                    except Exception as exc:  # noqa: BLE001 - preserve the run failure.
                        cleanup_exception = exc
                        break
                if cleanup_exception is None:
                    try:
                        cleanup_task.result()
                    except asyncio.CancelledError as exc:
                        cleanup_exception = exc
                    except Exception as exc:  # noqa: BLE001 - preserve the run failure.
                        cleanup_exception = exc
            finally:
                if not cleanup_task.done():
                    cleanup_task.cancel()
            if cleanup_exception is not None:
                teardown_error = f"{type(cleanup_exception).__name__}: details omitted"
                trace.emit(
                    "benchmark.teardown.error",
                    data={
                        "error_type": type(cleanup_exception).__name__,
                        "message": "benchmark teardown failed; details omitted",
                    },
                )
            current_task = asyncio.current_task()
            if interrupted or (current_task is not None and current_task.cancelling()):
                raise asyncio.CancelledError

        try:
            started = time.monotonic()
            root_goal = Goal(
                id=f"goal:{session.case_id}:objective",
                description=session.objective.description,
                status="active",
                priority=1.0,
                attributes={"benchmark": session.benchmark, "case_id": session.case_id},
            )
            targets = [
                Entity(
                    id=target.id,
                    type="benchmark.target",
                    attributes={
                        "address": target.address,
                        **sanitize_observability_data(target.metadata),
                    },
                )
                for target in session.targets
            ]
            # Every competing session attaches to the same authoritative world
            # but keeps its own progress ledger, views and checkpoint. The first
            # session seeds the world; the rest only attach control state.
            runtimes: list[_ExecutorRuntime] = []
            world = None
            registry = None
            for planned in plan.sessions:
                attach = world is not None
                session_runtime = bootstrap_solver(
                    run_dir=planned.root,
                    trace=trace,
                    goal=root_goal,
                    actor=f"agent:{planned.session_id}",
                    context_query=session.objective.description,
                    targets=() if attach else targets,
                    target_actor=f"benchmark:{session.benchmark}",
                    budget_limits=enforced_budget_limits(planned.budgets),
                    agent_workspace=planned.workspace,
                    finalize_requests_submission=True,
                    submission_due_fraction=BENCHMARK_SUBMISSION_DUE_FRACTION,
                    world_repository_factory=(
                        (lambda _path, shared=world: shared) if attach else SQLiteWorldRepository
                    ),
                    verifier_registry=registry,
                    seed_world=not attach,
                )
                if world is None:
                    world = session_runtime.world
                    registry = session_runtime.verifier_registry
                runtimes.append(
                    _ExecutorRuntime(
                        plan=planned,
                        task=TaskSpec(
                            apiVersion="harness/v1",
                            id=session.case_id,
                            name=session.case_id,
                            category=session.benchmark,
                            difficulty=case.difficulty,
                            objective=session.objective,
                            budgets=planned.budgets,
                        ),
                        runtime=session_runtime,
                        progress_path=planned.root / "progress.json",
                        submission_inbox=SubmissionInbox(
                            planned.workspace / "submission.inbox.jsonl"
                        ),
                    )
                )
            solver_loop = runtimes[0].runtime.loop
            context_builder = runtimes[0].runtime.context_builder
            progress = (
                ProgressLedger(
                    active_goal=root_goal.id,
                    budget_limits=enforced_budget_limits(budgets),
                    wall_time_grace_seconds=FINALIZE_GRACE_SECONDS,
                )
                if plan.competing
                else runtimes[0].runtime.progress
            )
            progress_path = run_dir / "progress.json"
            progress.write(progress_path)
            for planned in runtimes:
                sync_agent_workspace(run_dir=planned.plan.root, workspace=planned.plan.workspace)
            benchmark_verifier_authority = registry.register("benchmark_evaluator")

            trace.emit(
                "run.started",
                data={
                    "benchmark": session.benchmark,
                    "case_id": session.case_id,
                    "agent_id": agent.id,
                    "seed": seed,
                    "world_revision": world.snapshot.revision,
                },
            )
            trace.emit(
                "executor.sessions.planned",
                actor="harness",
                data={
                    "count": len(runtimes),
                    "competing": plan.competing,
                    "shared_findings": str(shared_findings) if shared_findings else None,
                    "sessions": [
                        {
                            "executor_id": item.plan.executor_id,
                            "session_id": item.plan.session_id,
                            "model": item.plan.agent.pi.model if item.plan.agent.pi else None,
                            "root": item.plan.root.name,
                        }
                        for item in runtimes
                    ],
                },
            )

            agent_result: AgentResult | None = None
            submission_results: list[dict] = []
            submitted_keys: set[tuple[str, str]] = set()
            evaluation: EvaluationResult | None = None
            evaluation_error: Exception | None = None

        except asyncio.CancelledError:
            if not lifecycle.terminal:
                lifecycle.settle(succeeded=False, cancelled=True)
            await teardown_once()
            raise
        except BaseException:
            if not lifecycle.terminal:
                lifecycle.settle(succeeded=False)
            await teardown_once()
            raise

        async def evaluate_final() -> bool:
            nonlocal evaluation, evaluation_error
            if evaluation_error is not None:
                return False
            try:
                evaluation = await _evaluate_benchmark(
                    adapter,
                    session,
                    timeout=budgets.wall_time - (time.monotonic() - started),
                )
            except Exception as exc:  # noqa: BLE001 - unavailable evaluators fail closed.
                evaluation_error = exc
                return False
            return evaluation.success

        def refresh_session_views() -> None:
            """Re-render the world projection for the run and every session."""
            for planned in runtimes:
                builder = planned.runtime.context_builder
                focus = builder.build_focus(
                    world.snapshot,
                    reference=session.objective.description,
                )
                (planned.plan.root / "world.context.txt").write_text(
                    builder.render(
                        world.snapshot,
                        query=session.objective.description,
                        focus=focus,
                    ),
                    encoding="utf-8",
                )
                sync_agent_workspace(run_dir=planned.plan.root, workspace=planned.plan.workspace)

        async def submit_candidate(
            submission: Submission,
            executor: _ExecutorRuntime | None = None,
        ) -> bool:
            nonlocal evaluation
            key = (submission.type, submission.value)
            if key in submitted_keys:
                return False
            submitted_keys.add(key)
            if executor is not None and executor.first_submission_at is None:
                executor.first_submission_at = time.monotonic()
            evaluation = None
            trace.emit(
                "benchmark.submission.proposed",
                actor="agent",
                data={
                    "type": submission.type,
                    "value_sha256": _submission_hash(submission.value),
                    "executor_id": executor.plan.executor_id if executor else None,
                },
            )
            submitted = await adapter.submit(session, submission)
            safe_submission_metadata = sanitize_observability_data(submitted.metadata)
            # The submitting session owns the stall signal (a rejected candidate
            # is a wrong lead), and the run ledger folds the session counters
            # once at the end, so submissions are recorded exactly once.
            submission_ledger = (
                executor.runtime.progress
                if executor is not None and plan.competing
                else progress
            )
            submission_ledger.record_submission(
                accepted=submitted.accepted,
                completed=submitted.completed,
            )
            submission_ledger.write(
                executor.progress_path
                if executor is not None and plan.competing
                else progress_path
            )
            submission_digest = _submission_hash(submission.value)
            data = submitted.model_dump()
            data["metadata"] = safe_submission_metadata
            data["value_sha256"] = submission_digest
            submission_results.append(data)
            trace.emit("benchmark.submission.result", data=data)
            # The platform verdict is the one thing in a benchmark run that the
            # Harness observes for itself. Recording it gives every session a
            # comparison baseline for its next action verdict instead of the
            # Agent's own expected-observation text (VER-05).
            for observing in runtimes:
                note_verdict = getattr(observing.runtime.loop, "note_observation", None)
                if not callable(note_verdict):
                    continue
                try:
                    note_verdict(
                            ObservationRecord(
                                observation_id=f"submission:{submission_digest[:16]}",
                                run_id=trace.run_id,
                                producer="benchmark_evaluator",
                                captured_at=datetime.now(UTC),
                                world_revision=world.snapshot.revision,
                                kind="benchmark.submission.verdict",
                                summary=(
                                    "platform "
                                    f"{'accepted' if submitted.accepted else 'rejected'} "
                                    f"a {submission.type} candidate "
                                    f"(score delta {submitted.score_delta}, "
                                    f"completed={submitted.completed})"
                                ),
                            )
                        )
                except (TypeError, ValueError):
                    # Telemetry for the next verdict, never a control path.
                    continue
            submission_completed = False
            if submitted.completed:
                # The submission response is an intermediate signal. Stop only
                # after the adapter's independent final evaluator agrees.
                submission_completed = await evaluate_final()

            feedback_id = f"benchmark-feedback:{submission_digest[:16]}"
            world.upsert(
                "observation",
                Observation(
                    id=feedback_id,
                    type="benchmark.submission.feedback",
                    content={
                        "submission_type": submission.type,
                        "value_sha256": submission_digest,
                        "accepted": submitted.accepted,
                        "score_delta": submitted.score_delta,
                        "completed": submitted.completed,
                        "metadata": safe_submission_metadata,
                    },
                    confidence=1.0,
                    source=session.benchmark,
                ),
                actor=f"benchmark:{session.benchmark}",
            )
            if not submitted.accepted:
                world.upsert(
                    "failure",
                    Failure(
                        id=f"benchmark-rejection:{submission_digest[:16]}",
                        type="benchmark.candidate_rejected",
                        message="Benchmark rejected submitted candidate",
                        recoverable=True,
                        attributes={
                            "submission_type": submission.type,
                            "value_sha256": submission_digest,
                        },
                    ),
                    actor=f"benchmark:{session.benchmark}",
                )
            revision_after_feedback = world.snapshot.revision
            refresh_session_views()

            feedback_data = {
                "submission_type": submission.type,
                "accepted": submitted.accepted,
                "score_delta": submitted.score_delta,
                "completed": submitted.completed,
                "metadata": safe_submission_metadata,
                "value_sha256": submission_digest,
                "world_revision": revision_after_feedback,
            }
            # Only the submitting session gets the immediate feedback pointer; the
            # other sessions see the new world revision through their own context.
            if executor is not None and executor.session is not None:
                executor.runtime.loop.note_delivered_steer("verification")
                await executor.session.observe(
                    AgentObservation(
                        type="benchmark.feedback",
                        data=feedback_data,
                    )
                )
            return submission_completed

        async def run_executor(executor: _ExecutorRuntime) -> None:
            """Drive one competing session to its own stop or the objective win."""
            planned = executor.plan
            executor.status = "running"
            trace.emit(
                "executor.session.started",
                actor="harness",
                data={
                    "executor_id": planned.executor_id,
                    "session_id": planned.session_id,
                    "model": planned.agent.pi.model if planned.agent.pi else None,
                    "budgets": planned.budgets.model_dump(mode="json"),
                },
            )
            adapter_instance = build_agent_adapter(planned.agent, trace=trace)
            capabilities = adapter_instance.capabilities
            trace.emit(
                "agent.capabilities",
                actor="harness",
                data={
                    "executor_id": planned.executor_id,
                    "live_steer": capabilities.live_steer,
                    "typed_event_channel": capabilities.typed_event_channel,
                    "submission_channels": list(capabilities.submission_channels),
                },
            )
            # Externally provisioned benchmarks have no verifier-bearing task
            # tree to sanitize; the session workspace is the task view. Passing
            # it directly avoids the local-run guard that rejects a task
            # directory nested under the authoritative run root.
            run_context = AgentRunContext(
                task=executor.task,
                task_dir=planned.workspace,
                agent_task_dir=planned.workspace,
                run_dir=planned.workspace,
                seed=seed,
                shared_findings_dir=plan.shared_dir,
            )

            executor.session = await adapter_instance.start_session(run_context)
            try:
                await save_session_checkpoint(
                    run_dir=planned.root,
                    session=executor.session,
                    world_revision=world.snapshot.revision,
                    progress=executor.runtime.progress,
                    processed_event_ids=executor.runtime.loop.processed_event_ids,
                )
                startup_decision = executor.runtime.loop.decide()
                if startup_decision.action.value == "stop":
                    trace.emit(
                        "solver.decision",
                        actor="harness",
                        data={
                            "action": "stop",
                            "reason": startup_decision.reason,
                            "executor_id": planned.executor_id,
                        },
                    )
                    await executor.session.observe(
                        AgentObservation(
                            type="solver.stop", data={"reason": startup_decision.reason}
                        )
                    )
                    await executor.session.close(
                        f"solver-stop:{startup_decision.reason}"
                    )
                else:
                    while True:
                        restarted = await self._consume_session(
                            executor=executor,
                            planned=planned,
                            run_context=run_context,
                            adapter_instance=adapter_instance,
                            trace=trace,
                            world=world,
                            archive=archive,
                            submit_candidate=submit_candidate,
                        )
                        if not restarted:
                            break
                executor.result = await executor.session.result()
                if executor.retired_results:
                    executor.result = _merge_results(
                        [*executor.retired_results, executor.result]
                    )
                await save_session_checkpoint(
                    run_dir=planned.root,
                    session=executor.session,
                    world_revision=world.snapshot.revision,
                    progress=executor.runtime.progress,
                    budget_used=executor.result.metrics.as_dict(),
                    processed_event_ids=executor.runtime.loop.processed_event_ids,
                )
            except BaseException as stream_exc:
                if not executor.completed:
                    close = getattr(executor.session, "close", None)
                    if callable(close):
                        try:
                            close_reason = (
                                "cancelled"
                                if isinstance(stream_exc, asyncio.CancelledError)
                                else "benchmark_run_error"
                            )
                            await close(close_reason)
                        except Exception as close_exc:  # noqa: BLE001 - preserve original failure
                            trace.emit(
                                "agent.close_error",
                                data={
                                    "executor_id": planned.executor_id,
                                    "error_type": type(close_exc).__name__,
                                    "message": str(close_exc),
                                },
                            )
                raise

            executor.runtime.loop.finish_ingest()

            # The Agent's final report is untrusted text: the Harness records
            # which of the four requested sections are missing so a thin
            # hand-off is visible in the run result instead of silently lost.
            summary_path = planned.workspace / "agent.summary.md"
            executor.summary_missing_sections = missing_summary_sections(summary_path)
            if executor.summary_missing_sections:
                trace.emit(
                    "solver.summary_incomplete",
                    actor="harness",
                    data={
                        "executor_id": planned.executor_id,
                        "reason": (
                            "summary_missing"
                            if not summary_path.is_file()
                            else "sections_missing"
                        ),
                        "sections_missing": executor.summary_missing_sections,
                    },
                )

            if not executor.completed:
                for submission in executor.submission_inbox.poll():
                    executor.submissions += 1
                    if await submit_candidate(submission, executor):
                        executor.completed = True
                        break

            if not executor.completed and executor.result is not None:
                for submission in submission_extractor(executor.result):
                    executor.submissions += 1
                    if await submit_candidate(submission, executor):
                        executor.completed = True
                        break

            executor.status = "completed" if executor.completed else "finished"
            trace.emit(
                "executor.session.finished",
                actor="harness",
                data={
                    "executor_id": planned.executor_id,
                    "status": executor.status,
                    "submissions": executor.submissions,
                },
            )

        action_owner = run_action_owner(run_dir)
        action_owner_acquired = False
        try:
            action_owner.__enter__()
            action_owner_acquired = True
            if plan.competing:
                await _race_sessions(runtimes, run_executor, trace)
                if archive is not None:
                    archive.snapshot()
            else:
                await run_executor(runtimes[0])

            if plan.competing:
                session_results = [
                    item.result for item in runtimes if item.result is not None
                ]
                if not session_results:
                    raise RuntimeError("agent did not start")
                agent_result = _merge_results(session_results)
                fold_session_progress(
                    progress, (item.runtime.progress for item in runtimes)
                )
            else:
                agent_result = runtimes[0].result
                if agent_result is None:
                    raise RuntimeError("agent did not start")

            if evaluation is None:
                await evaluate_final()
            lifecycle.transition("verifying")
            # The final evaluator is authoritative. A submission marked
            # completed is feedback, not a substitute for this result.
            if evaluation_error is None and evaluation is not None:
                persist_objective_verdict(
                    run_dir=run_dir,
                    run_id=run_id,
                    world_revision=world.snapshot.revision,
                    producer_authority=benchmark_verifier_authority,
                    registry=registry,
                    progress=progress,
                    trace=trace,
                    success=evaluation.success,
                    score=evaluation.score,
                    milestones=evaluation.milestones,
                )
            else:
                persist_objective_verdict(
                    run_dir=run_dir,
                    run_id=run_id,
                    world_revision=world.snapshot.revision,
                    producer_authority=benchmark_verifier_authority,
                    registry=registry,
                    progress=progress,
                    trace=trace,
                    status="unavailable",
                    failure_type=type(evaluation_error).__name__
                    if evaluation_error is not None
                    else "RuntimeError",
                )
                evaluation = EvaluationResult(
                    success=False,
                    score=0.0,
                    message="benchmark evaluator unavailable",
                    metadata={
                        "available": False,
                        "error_type": type(evaluation_error).__name__
                        if evaluation_error is not None
                        else "RuntimeError",
                    },
                )
            progress.write(progress_path)
        except asyncio.CancelledError:
            if not lifecycle.terminal:
                lifecycle.settle(succeeded=False, cancelled=True)
            raise
        except Exception:
            if not lifecycle.terminal:
                lifecycle.settle(succeeded=False)
            raise
        finally:
            try:
                await teardown_once()
            finally:
                if action_owner_acquired:
                    action_owner.__exit__(None, None, None)

        if agent_result is None:
            raise RuntimeError("agent did not start")

        # The Pi session may be stopped by the solver's own budget check before
        # the RPC session records its own exhaustion, so treat an empty remaining
        # budget as exhaustion too. Otherwise a budget stop would be reported as
        # an ordinary solver failure.
        budget_exhausted = (
            bool(agent_result.budget_exceeded) or progress.remaining_budget_fraction <= 0.0
        )
        status = (
            "error"
            if evaluation_error is not None or evaluation is None
            else resolve_run_status(
                objective_completed=progress.objective_completed,
                timed_out=agent_result.timed_out,
                budget_exceeded=budget_exhausted,
            )
        )
        lifecycle.settle(
            succeeded=progress.objective_completed,
            exhausted=not progress.objective_completed
            and (agent_result.timed_out or budget_exhausted),
        )
        finalize_world_goal(
            world=world,
            goal=root_goal,
            context_builder=context_builder,
            run_dir=run_dir,
            status=status,
            success=bool(evaluation and evaluation.success),
            score=evaluation.score if evaluation is not None else 0.0,
        )
        world.flush()

        termination_reason = (
            "objective-complete"
            if progress.objective_completed
            else "budget-exhausted"
            if budget_exhausted
            else None
        )
        # The winning session owns the run narrative; a run with no winner falls
        # back to the first session so the artifact is deterministic.
        report_executor = next(
            (item for item in runtimes if item.completed), runtimes[0]
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        first_submission_ms = _first_submission_ms(runtimes, started)
        summary = run_summary(
            world=world,
            progress=progress,
            run_dir=run_dir,
            agent_report_path=report_executor.plan.workspace / "agent.summary.md",
            outcome={
                "status": status,
                "success": bool(evaluation and evaluation.success),
                "score": evaluation.score if evaluation is not None else 0.0,
                "termination_reason": termination_reason,
            },
        )
        result = {
            "schema_version": "harness/result/v2",
            "run_id": run_id,
            "benchmark": {
                "type": session.benchmark,
                "case_id": session.case_id,
                "domain": case.domain,
                "difficulty": case.difficulty,
                "session": sanitize_observability_data(session.metadata),
            },
            "agent_id": agent.id,
            "status": status,
            "termination_reason": (
                termination_reason
            ),
            "summary": summary,
            "agent_report": {
                "present": (
                    report_executor.plan.workspace / "agent.summary.md"
                ).is_file(),
                "sections_missing": list(report_executor.summary_missing_sections),
            },
            "success": bool(evaluation and evaluation.success),
            "score": evaluation.score if evaluation is not None else 0.0,
            "message": (
                sanitize_observability_data({"message": evaluation.message}).get("message")
                if evaluation is not None
                else "benchmark evaluator unavailable"
            ),
            "milestones": (
                sanitize_observability_data(evaluation.milestones) if evaluation is not None else {}
            ),
            "evaluation": (
                sanitize_observability_data(evaluation.metadata) if evaluation is not None else {}
            ),
            "submissions": submission_results,
            "cleanup": {
                "ok": teardown_error is None,
                "error": teardown_error,
            },
            "world": world_result(
                world=world,
                solver_loop=solver_loop,
                agent_id=agent.id,
                agent_ids=[item.plan.session_id for item in runtimes] if plan.competing else None,
                extra_solver_loops=(
                    [item.runtime.loop for item in runtimes[1:]] if plan.competing else ()
                ),
            ),
            "network": network_result(agent),
            "progress": progress.model_dump(mode="json"),
            "metrics": {
                "duration_ms": duration_ms,
                **agent_result.metrics.as_dict(),
                # Attempted candidates, not accepted ones: a run that never
                # submitted anything fails differently from a run whose guesses
                # were rejected, and the sweep summary needs that distinction.
                "submissions_attempted": sum(item.submissions for item in runtimes),
                "first_submission_ms": first_submission_ms,
                # Where in the wall clock the first candidate went out: the
                # winning TSec runs of 2026-10-16 submitted at 0.99, i.e. the
                # platform verdict arrived after their own window had closed.
                "submitted_at_fraction": submission_fraction(
                    first_submission_ms, duration_ms
                ),
                # Steer volume by bucket, tool time and the final prompt size:
                # the three numbers the sweep review had to recompute by hand.
                **_merged_loop_metrics(runtimes),
            },
        }
        restart_briefs = [
            {
                "executor_id": item.plan.executor_id,
                "session_id": item.plan.session_id,
                **entry,
            }
            for item in runtimes
            for entry in item.restart_briefs
        ]
        if restart_briefs:
            result["restart_briefs"] = restart_briefs
        if plan.competing:
            result["executor_sessions"] = [
                {
                    "id": item.plan.session_id,
                    "executor_id": item.plan.executor_id,
                    "index": item.plan.index,
                    "agent_id": item.plan.agent.id,
                    "model": item.plan.agent.pi.model if item.plan.agent.pi else None,
                    "provider": item.plan.agent.pi.provider if item.plan.agent.pi else None,
                    "prompt": (
                        item.plan.agent.pi.system_prompt_file
                        if item.plan.agent.pi
                        else None
                    ),
                    "status": item.status,
                    "objective_completed": item.completed,
                    "submissions": item.submissions,
                    "budget_used": dict(item.runtime.progress.budget_used),
                    "metrics": item.result.metrics.as_dict() if item.result else {},
                    "summary_present": (
                        item.plan.workspace / "agent.summary.md"
                    ).is_file(),
                    "summary_sections_missing": list(item.summary_missing_sections),
                    **(
                        {"restart_briefs": item.restart_briefs}
                        if item.restart_briefs
                        else {}
                    ),
                }
                for item in runtimes
            ]
            result["executor_summary"] = _executor_summary(runtimes)
        persist_run_result(run_dir=run_dir, trace=trace, result=result)
        return result
