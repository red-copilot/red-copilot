"""Sweep orchestration: three challenges, three solvers each, one blackboard.

One task = one provisioned challenge instance + three Pi containers racing on
it. One sweep = three tasks, capped by the platform's instance limit. Every
task runs the same fixed round loop: merge the board, render the graph, apply
goal ops, submit any new flag candidate, then brief all three solvers at once.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .blackboard import (
    INBOX_DIRNAME,
    SNAPSHOT_DIRNAME,
    BoardSnapshot,
    BoardWriter,
    crossed_sessions,
    flag_candidates,
    merge_reads,
    read_inbox,
    render_graph,
)
from .containers import (
    CONTAINER_GOALS,
    CONTAINER_GRAPH,
    CONTAINER_INBOX,
    CONTAINER_KNOWLEDGE,
    CONTAINER_NOTES,
    ContainerError,
    ContainerSpec,
    Mount,
    PiRpcSession,
    create_command,
    current_user,
    destroy_container,
    ensure_container,
    passthrough_env,
    solver_env,
)
from .goals import GoalOpRead, GoalStore
from .models import (
    DEFAULT_TOOLS,
    MAX_SESSIONS_PER_TASK,
    Challenge,
    LayoutError,
    SpikeError,
    select_challenges,
)
from .platform import (
    PlatformClient,
    PlatformError,
    PlatformUnusable,
    load_config,
    platform_failure_kind,
)
from .rounds import (
    RoundOutcome,
    goal_lines_for_prompt,
    initial_prompt,
    read_system_prompt,
    render_objective,
    round_brief,
    status_record,
)

SESSION_IDS = ("s1", "s2", "s3")
SOLVER_THINKING = ("high", "medium", "low")
GRAPH_HEADER = "# Intent-fact graph\n\n(no round has been rendered yet)\n"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PLATFORM = 3
EXIT_NO_RUN = 4


@dataclass(frozen=True)
class SweepConfig:
    runs_root: Path
    agent_dir: Path
    knowledge_roots: tuple[Path, ...]
    image: str
    provider: str
    model: str
    tools: tuple[str, ...] = DEFAULT_TOOLS
    challenge_count: int = len(SESSION_IDS)
    parallel: int = len(SESSION_IDS)
    wall_time: int = 3600
    round_seconds: int = 180
    passthrough: tuple[str, ...] = ()
    base_url_env: str = "BENCHMARK_BASE_URL"
    token_env: str = "BENCHMARK_TOKEN"
    challenges: tuple[str, ...] = ()
    plan_only: bool = False

    @property
    def session_ids(self) -> tuple[str, ...]:
        return SESSION_IDS[:MAX_SESSIONS_PER_TASK]


@dataclass(frozen=True)
class SessionPaths:
    session_id: str
    index: int
    directory: Path
    workspace: Path
    notes: Path
    inbox_file: Path
    goals_file: Path
    stdout_log: Path
    stderr_log: Path
    status_log: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "session": self.session_id,
            "directory": str(self.directory),
            "inbox": str(self.inbox_file),
            "goals": str(self.goals_file),
        }


@dataclass(frozen=True)
class RunLayout:
    root: Path
    code: str

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def inbox_dir(self) -> Path:
        return self.shared_dir / INBOX_DIRNAME

    @property
    def goals_dir(self) -> Path:
        return self.shared_dir / "goals"

    @property
    def snapshots_dir(self) -> Path:
        return self.shared_dir / SNAPSHOT_DIRNAME

    @property
    def graph_path(self) -> Path:
        return self.shared_dir / "intent-fact-graph.md"

    @property
    def goal_journal(self) -> Path:
        return self.root / "goal.jsonl"

    @property
    def platform_log(self) -> Path:
        return self.root / "platform.jsonl"

    @property
    def result_path(self) -> Path:
        return self.root / "result.json"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    def prepare(self) -> None:
        self._archive_previous_attempt()
        for path in (self.root, self.shared_dir, self.inbox_dir, self.goals_dir, self.snapshots_dir):
            if path.is_symlink():
                raise LayoutError(f"refusing to use a symlinked run path: {path}")
            path.mkdir(parents=True, exist_ok=True)
        if not self.graph_path.exists():
            self.graph_path.write_text(GRAPH_HEADER, encoding="utf-8")
        for index, session_id in enumerate(SESSION_IDS, start=1):
            session = self.session(index=index, session_id=session_id)
            session.workspace.mkdir(parents=True, exist_ok=True)
            session.notes.mkdir(parents=True, exist_ok=True)
            # Pi needs a writable HOME and config/session dirs inside the
            # read-only root, so they are created on the writable mount.
            for name in ("home", ".pi-agent", ".pi-sessions"):
                (session.workspace / name).mkdir(parents=True, exist_ok=True)
            session.inbox_file.touch()
            session.goals_file.touch()
        (self.sessions_dir / ".keep").touch()

    def _archive_previous_attempt(self) -> None:
        """Keep a repeated sweep from replaying the previous attempt's goals.

        ``goal.jsonl`` is authoritative for one attempt, so a finished attempt
        is moved aside instead of being overwritten or replayed into the next.
        """

        if not (self.root / "result.json").exists():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.root / "archive" / stamp
        suffix = 1
        while target.exists():
            suffix += 1
            target = self.root / "archive" / f"{stamp}-{suffix}"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("result.json", "goal.jsonl", "platform.jsonl", "shared", "sessions"):
            path = self.root / name
            if path.exists():
                path.rename(target / name)

    def session(self, *, index: int, session_id: str) -> SessionPaths:
        directory = self.sessions_dir / f"{index:02d}-{session_id}"
        return SessionPaths(
            session_id=session_id,
            index=index,
            directory=directory,
            workspace=directory / "workspace",
            notes=directory / "notes",
            inbox_file=self.inbox_dir / f"{session_id}.md",
            goals_file=self.goals_dir / f"{session_id}.jsonl",
            stdout_log=directory / "pi.stdout.log",
            stderr_log=directory / "pi.stderr.log",
            status_log=directory / "status.jsonl",
        )


@dataclass
class SessionState:
    session_id: str
    index: int
    paths: SessionPaths
    container: str
    session: PiRpcSession
    restarts: int = 0
    rounds: int = 0
    error_rounds: int = 0
    exit_reason: str = "running"
    last_status: dict[str, object] | None = None

    @property
    def alive(self) -> bool:
        return self.session.alive

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.session_id,
            "container": self.container,
            "provider": self.session.provider,
            "model": self.session.model,
            "thinking": self.session.thinking,
            "rounds": self.rounds,
            "error_rounds": self.error_rounds,
            "restarts": self.restarts,
            "exit_reason": self.exit_reason,
            "session_id": self.session.session_id,
            **self.session.counters.as_dict(),
        }


@dataclass
class TaskOutcome:
    code: str
    difficulty: str
    status: str
    exit_reason: str
    rounds: int
    started: bool
    addresses: tuple[str, ...] = ()
    wall_clock_seconds: float = 0.0
    flags: list[dict[str, object]] = field(default_factory=list)
    sessions: list[dict[str, object]] = field(default_factory=list)
    goal: dict[str, object] = field(default_factory=dict)
    blackboard: dict[str, object] = field(default_factory=dict)
    platform_events: list[dict[str, object]] = field(default_factory=list)

    @property
    def wrote_run(self) -> bool:
        """True only when a round actually completed inside a session."""

        return self.rounds >= 1 and any(
            int(session.get("rounds", 0) or 0) > 0 for session in self.sessions
        )

    def as_result(self) -> dict[str, object]:
        return {
            "challenge": self.code,
            "difficulty": self.difficulty,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "rounds": self.rounds,
            "wall_clock_seconds": round(self.wall_clock_seconds, 3),
            "target_addresses": list(self.addresses),
            "flags": self.flags,
            "sessions": self.sessions,
            "goal": self.goal,
            "blackboard": self.blackboard,
        }


@dataclass
class SweepReport:
    started_at: str
    finished_at: str = ""
    selection: list[dict[str, object]] = field(default_factory=list)
    outcomes: list[TaskOutcome] = field(default_factory=list)
    platform_kind: str | None = None
    message: str = ""

    @property
    def exit_code(self) -> int:
        return exit_code_for(platform_kind=self.platform_kind, outcomes=self.outcomes)

    def as_dict(self) -> dict[str, object]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "platform_failure": self.platform_kind,
            "message": self.message,
            "selection": self.selection,
            "exit_code": self.exit_code,
            "tasks": [
                {
                    "challenge": outcome.code,
                    "status": outcome.status,
                    "exit_reason": outcome.exit_reason,
                    "rounds": outcome.rounds,
                    "wrote_run": outcome.wrote_run,
                }
                for outcome in self.outcomes
            ],
        }


def exit_code_for(*, platform_kind: str | None, outcomes: Sequence[TaskOutcome]) -> int:
    """Map a finished sweep onto the operator-facing exit code."""

    if platform_kind is not None:
        return EXIT_PLATFORM
    if not outcomes:
        return EXIT_NO_RUN
    if any(not outcome.wrote_run for outcome in outcomes):
        return EXIT_NO_RUN
    return EXIT_OK


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def session_spec(
    *,
    config: SweepConfig,
    layout: RunLayout,
    paths: SessionPaths,
    challenge: str,
    objective: str,
    addresses: Sequence[str],
    round_seconds: int,
) -> ContainerSpec:
    mounts = [
        Mount(host=paths.workspace, container="/workspace"),
        Mount(host=paths.notes, container=CONTAINER_NOTES),
        Mount(host=layout.inbox_dir, container=CONTAINER_INBOX),
        Mount(host=layout.goals_dir, container=CONTAINER_GOALS),
        Mount(host=layout.graph_path, container=CONTAINER_GRAPH, read_only=True),
    ]
    for index, root in enumerate(config.knowledge_roots, start=1):
        name = root.name or f"root-{index}"
        mounts.append(Mount(host=root, container=f"{CONTAINER_KNOWLEDGE}/{name}", read_only=True))
    # The solver contract and the blackboard protocol are part of the agent's
    # read-only knowledge, not only of the runtime's prompt.
    mounts.append(
        Mount(host=config.agent_dir, container=f"{CONTAINER_KNOWLEDGE}/spike", read_only=True)
    )
    environment = solver_env(
        session_id=paths.session_id,
        challenge=challenge,
        objective=objective,
        inbox_file=f"{CONTAINER_INBOX}/{paths.session_id}.md",
        goals_file=f"{CONTAINER_GOALS}/{paths.session_id}.jsonl",
        round_seconds=round_seconds,
        addresses=addresses,
    )
    environment = environment + passthrough_env(config.passthrough)
    return ContainerSpec(
        name=container_name(challenge, paths.session_id),
        image=config.image,
        mounts=tuple(mounts),
        environment=tuple(environment),
        user=current_user(),
    )


async def launch_session(
    *,
    config: SweepConfig,
    layout: RunLayout,
    paths: SessionPaths,
    challenge: str,
    objective: str,
    addresses: Sequence[str],
    thinking: str,
) -> SessionState:
    spec = session_spec(
        config=config,
        layout=layout,
        paths=paths,
        challenge=challenge,
        objective=objective,
        addresses=addresses,
        round_seconds=config.round_seconds,
    )
    await ensure_container(spec)
    session = PiRpcSession(
        container=spec.name,
        provider=config.provider,
        model=config.model,
        thinking=thinking,
        tools=config.tools,
        log_path=paths.stderr_log,
        raw_log_path=paths.stdout_log,
    )
    await session.start()
    return SessionState(
        session_id=paths.session_id,
        index=paths.index,
        paths=paths,
        container=spec.name,
        session=session,
    )


async def replace_session(state: SessionState, *, config: SweepConfig) -> None:
    """One resume attempt per session, reusing the Pi session id."""

    if state.restarts >= 1:
        state.exit_reason = "crashed"
        return
    previous_id = state.session.session_id
    log_path = state.session.log_path
    await state.session.stop()
    replacement = PiRpcSession(
        container=state.container,
        provider=config.provider,
        model=config.model,
        thinking=state.session.thinking,
        tools=config.tools,
        log_path=log_path,
        raw_log_path=state.paths.stdout_log,
        session_id=previous_id,
    )
    try:
        await replacement.start(require_session=previous_id is not None)
    except ContainerError:
        state.exit_reason = "restart_failed"
        state.session = replacement
        return
    state.restarts += 1
    state.session = replacement
    state.exit_reason = "running"


async def run_task(
    *,
    client: PlatformClient,
    challenge: Challenge,
    config: SweepConfig,
) -> TaskOutcome:
    layout = RunLayout(root=config.runs_root / challenge.code, code=challenge.code)
    layout.prepare()
    started_at = time.monotonic()
    outcome = TaskOutcome(
        code=challenge.code,
        difficulty=challenge.difficulty_label,
        status="failed",
        exit_reason="not_started",
        rounds=0,
        started=False,
    )
    goal_store = GoalStore(
        code=challenge.code,
        description=challenge.description or challenge.code,
        journal=layout.goal_journal,
    )
    board = BoardWriter(graph_path=layout.graph_path, snapshots_dir=layout.snapshots_dir)
    states: list[SessionState] = []
    snapshot = BoardSnapshot(
        round=0, facts=(), intents=(), questions=(), flags=(), notes=(), unparsed=()
    )
    goal_offsets: dict[str, int] = {session_id: 0 for session_id in config.session_ids}
    submitted: set[str] = set()
    delta: dict[str, int] = {"new_facts": 0, "new_corroborated": 0, "flag_candidates": 0}
    previous_facts = 0
    previous_corroborated = 0

    try:
        try:
            started = await client.start_challenge(challenge.code)
        except PlatformUnusable as exc:
            # Platform-wide: the sweep exits, it never retries in a loop.
            outcome.status = "failed"
            outcome.exit_reason = f"platform:{exc.kind}"
            raise
        except Exception as exc:  # noqa: BLE001 - classified and recorded below
            outcome.status = "failed"
            outcome.exit_reason = (
                f"platform:{platform_failure_kind(exc) or type(exc).__name__}"
            )
            return outcome
        outcome.started = True
        outcome.addresses = started.addresses
        goal_store.activate_root(round=0)
        objective = render_objective(challenge.code, challenge.description, started.addresses)
        try:
            system_prompt = read_system_prompt(config.agent_dir)
        except FileNotFoundError as exc:
            outcome.exit_reason = f"agent_assets_missing: {exc}"
            outcome.status = "failed"
            return outcome

        window_deadline = started_at + config.wall_time
        for index, session_id in enumerate(config.session_ids, start=1):
            thinking = SOLVER_THINKING[(index - 1) % len(SOLVER_THINKING)]
            paths = layout.session(index=index, session_id=session_id)
            state = await launch_session(
                config=config,
                layout=layout,
                paths=paths,
                challenge=challenge.code,
                objective=objective,
                addresses=started.addresses,
                thinking=thinking,
            )
            states.append(state)

        round_no = 0
        while True:
            # Every round begins with the board pass, so the lines the solvers
            # wrote during the previous round are merged, rendered and applied
            # before anyone is briefed again. The final pass also runs after the
            # window closes, which keeps the last round's findings on the record.
            round_no += 1
            outcome.rounds = round_no
            snapshot = _merge_board(states, round=round_no)
            corroborated = sum(1 for fact in snapshot.facts if fact.corroborated)
            delta = {
                "new_facts": max(0, len(snapshot.facts) - previous_facts),
                "new_corroborated": max(0, corroborated - previous_corroborated),
                "flag_candidates": len(flag_candidates(snapshot)),
            }
            previous_facts, previous_corroborated = len(snapshot.facts), corroborated
            graph_text = render_graph(
                snapshot,
                challenge=challenge.code,
                goal_lines=goal_lines_for_prompt(goal_store.render_lines()),
                sessions=config.session_ids,
            )
            publish = board.publish(round=round_no, text=graph_text)
            _apply_goal_ops(goal_store, states, snapshot, round=round_no, offsets=goal_offsets)
            accepted = await _submit_flags(
                client, challenge.code, snapshot, submitted, outcome, round=round_no
            )
            outcome.blackboard = _board_summary(board, snapshot, publish, round_no)
            if accepted is not None:
                goal_store.achieve_root(flag=accepted, round=round_no)
                outcome.status = "achieved"
                outcome.exit_reason = "flag_accepted"
                break
            if not any(state.alive for state in states):
                outcome.status = "blocked"
                outcome.exit_reason = "all_sessions_dead"
                goal_store.block_root(reason="all_sessions_dead", round=round_no)
                break
            if all(state.error_rounds >= 2 for state in states):
                # Two rounds in a row where every session failed at the provider
                # (missing credential, unsupported model): the loop would only
                # burn the window, so the task fails closed instead.
                outcome.status = "blocked"
                outcome.exit_reason = "provider_error"
                goal_store.block_root(reason="provider_error", round=round_no)
                break
            remaining = window_deadline - time.monotonic()
            if remaining <= 0:
                outcome.status = "abandoned"
                outcome.exit_reason = "window_elapsed"
                goal_store.abandon_root(reason="window_elapsed", round=round_no)
                break
            round_started = time.monotonic()
            await _run_round(
                states,
                config=config,
                challenge=challenge.code,
                objective=objective,
                system_prompt=system_prompt,
                goal_lines=goal_store.render_lines(),
                graph_text=graph_text,
                round=round_no,
                remaining=remaining,
                first_round=round_no == 1,
                delta=delta,
            )
            if not all(state.error_rounds > 0 for state in states):
                # Fixed cadence: hold the round open so all three solvers write
                # between the same two renders. A round in which every session
                # errored skips the pause so the fail-fast check trips at once.
                pause = config.round_seconds - (time.monotonic() - round_started)
                if pause > 0:
                    await asyncio.sleep(
                        min(pause, max(0.0, window_deadline - time.monotonic()))
                    )
    finally:
        for state in states:
            if state.exit_reason == "running":
                state.exit_reason = "closed"
            await state.session.stop()
            await destroy_container(state.container)
        if outcome.started:
            try:
                await client.close_challenge(challenge.code)
            except Exception as exc:  # noqa: BLE001 - closing must never mask the cause
                append_jsonl(
                    layout.platform_log,
                    {"event": "close_failed", "challenge": challenge.code, "detail": str(exc)},
                )
        try:
            _finalize(outcome, layout, goal_store, board, states, started_at, snapshot)
        except OSError as exc:  # pragma: no cover - disk full and friends
            append_jsonl(
                layout.platform_log,
                {"event": "finalize_failed", "challenge": challenge.code, "detail": str(exc)},
            )
    return outcome


def _merge_board(states: Sequence[SessionState], *, round: int) -> BoardSnapshot:
    reads = [
        read_inbox(session=state.session_id, path=state.paths.inbox_file, round=round)
        for state in states
    ]
    return merge_reads(reads, round=round)


def _apply_goal_ops(
    goal_store: GoalStore,
    states: Sequence[SessionState],
    snapshot: BoardSnapshot,
    *,
    round: int,
    offsets: dict[str, int],
) -> None:
    reads: list[GoalOpRead] = []
    for state in states:
        path = state.paths.goals_file
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
        consumed = offsets.get(state.session_id, 0)
        new_lines = lines[consumed:]
        offsets[state.session_id] = len(lines)
        if new_lines:
            reads.append(
                GoalOpRead(session=state.session_id, round=round, raw="\n".join(new_lines))
            )
    if reads:
        goal_store.apply_reads(
            reads, round=round, facts=snapshot.facts, sessions=[s.session_id for s in states]
        )


async def _submit_flags(
    client: PlatformClient,
    code: str,
    snapshot: BoardSnapshot,
    submitted: set[str],
    outcome: TaskOutcome,
    *,
    round: int,
) -> str | None:
    accepted: str | None = None
    for candidate in flag_candidates(snapshot):
        key = candidate.casefold()
        if key in submitted:
            continue
        submitted.add(key)
        try:
            verdict = await client.submit_flag(code, candidate)
        except PlatformUnusable:
            raise
        except Exception as exc:  # noqa: BLE001 - a rejected call must not kill the task
            outcome.flags.append(
                {
                    "value": candidate,
                    "round": round,
                    "accepted": False,
                    "error": type(exc).__name__,
                }
            )
            continue
        outcome.flags.append(
            {
                "value": candidate,
                "round": round,
                "accepted": verdict.accepted,
                "duplicate": verdict.duplicate,
                "awarded": verdict.awarded,
                "correct_flag_count": verdict.correct_flag_count,
                "total_flag_count": verdict.total_flag_count,
            }
        )
        if verdict.accepted and accepted is None:
            accepted = candidate
    return accepted


async def _run_round(
    states: Sequence[SessionState],
    *,
    config: SweepConfig,
    challenge: str,
    objective: str,
    system_prompt: str,
    goal_lines: Sequence[str],
    graph_text: str,
    round: int,
    remaining: float,
    first_round: bool,
    delta: dict[str, int],
) -> None:
    round_seconds = max(5, int(min(config.round_seconds, remaining)))
    peers = [state.session_id for state in states]
    briefs: list[str] = []
    for state in states:
        if first_round:
            brief = initial_prompt(
                system_prompt=system_prompt,
                objective=objective,
                session_id=state.session_id,
                peers=peers,
                round_seconds=round_seconds,
                inbox_file=f"{CONTAINER_INBOX}/{state.session_id}.md",
                goals_file=f"{CONTAINER_GOALS}/{state.session_id}.jsonl",
            )
        else:
            brief = round_brief(
                round=round,
                session_id=state.session_id,
                remaining_seconds=remaining,
                settled=state.session.settled,
                goal_lines=goal_lines,
                graph_text=graph_text,
                last_status=state.last_status,
                inbox_file=f"{CONTAINER_INBOX}/{state.session_id}.md",
                goals_file=f"{CONTAINER_GOALS}/{state.session_id}.jsonl",
            )
        briefs.append(brief)

    async def one(state: SessionState, brief: str) -> None:
        if not state.alive:
            await replace_session(state, config=config)
        if not state.alive:
            state.last_status = {"error": "session_dead", "reason": state.exit_reason}
            append_jsonl(state.paths.status_log, {"round": round, **state.last_status})
            return
        try:
            if first_round:
                await state.session.prompt(brief)
            else:
                await state.session.send_round(brief)
        except ContainerError as exc:
            state.exit_reason = f"send_failed: {exc}"
            return
        settled = await state.session.wait_settled(timeout=round_seconds)
        state.rounds += 1
        error = str(getattr(state.session, "last_error", "") or "")
        if error:
            state.error_rounds += 1
            state.exit_reason = "provider_error"
        else:
            state.error_rounds = 0
        outcome = RoundOutcome(
            round=round,
            session_id=state.session_id,
            settled=settled,
            alive=state.alive,
            inbox_lines=_inbox_lines(state),
            new_facts=int(delta.get("new_facts", 0)),
            new_corroborated=int(delta.get("new_corroborated", 0)),
            flag_candidates=int(delta.get("flag_candidates", 0)),
            counters=state.session.counters.as_dict(),
        )
        state.last_status = outcome.as_dict()
        record = status_record(outcome=outcome, brief_chars=len(brief))
        if error:
            state.last_status["error"] = error
            record["error"] = error
        append_jsonl(state.paths.status_log, record)

    await asyncio.gather(*(one(state, brief) for state, brief in zip(states, briefs)))


def _inbox_lines(state: SessionState) -> int:
    path = state.paths.inbox_file
    if not path.exists():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:  # pragma: no cover - defensive
        return 0


def _board_summary(
    board: BoardWriter,
    snapshot: BoardSnapshot,
    publish: dict[str, object],
    round_no: int,
) -> dict[str, object]:
    return {
        "rounds_rendered": board.renders,
        "render_round": round_no,
        "render_hash": publish.get("hash"),
        "render_bytes": publish.get("bytes"),
        "snapshot": publish.get("snapshot"),
        "clobber_incidents": board.clobber_incidents,
        "incidents": board.incidents,
        "cross_session_facts": crossed_sessions(snapshot),
        **snapshot.stats,
    }


def _finalize(
    outcome: TaskOutcome,
    layout: RunLayout,
    goal_store: GoalStore,
    board: BoardWriter,
    states: Sequence[SessionState],
    started_at: float,
    snapshot: BoardSnapshot,
) -> None:
    outcome.wall_clock_seconds = time.monotonic() - started_at
    outcome.sessions = [state.as_dict() for state in states]
    outcome.goal = goal_store.summary()
    # Always recompute from the last merged snapshot: the round that ends the
    # task is usually the round whose board actually mattered.
    summary: dict[str, object] = {
        "rounds_rendered": board.renders,
        "clobber_incidents": board.clobber_incidents,
        "incidents": board.incidents,
        "cross_session_facts": crossed_sessions(snapshot),
        **snapshot.stats,
    }
    for key in ("render_round", "render_hash", "render_bytes", "snapshot"):
        if key in outcome.blackboard:
            summary[key] = outcome.blackboard[key]
    outcome.blackboard = summary
    layout.result_path.write_text(
        json.dumps(outcome.as_result(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def run_sweep(config: SweepConfig) -> SweepReport:
    report = SweepReport(started_at=utc_now())
    config.runs_root.mkdir(parents=True, exist_ok=True)
    try:
        tsec_config = load_config(base_url_env=config.base_url_env, token_env=config.token_env)
    except PlatformError as exc:
        report.message = str(exc)
        report.platform_kind = "missing_credentials"
        report.finished_at = utc_now()
        return report

    try:
        async with PlatformClient(config=tsec_config) as client:
            challenges = await client.list_challenges()
            selection = select_challenges(
                challenges, count=config.challenge_count, overrides=config.challenges
            )
            report.selection = [
                {
                    "challenge": challenge.code,
                    "difficulty": challenge.difficulty_label,
                    "total_score": challenge.total_score,
                    "flag_count": challenge.flag_count,
                    "selection_key": list(challenge.selection_key),
                }
                for challenge in selection
            ]
            semaphore = asyncio.Semaphore(config.parallel)

            async def guarded(challenge: Challenge) -> TaskOutcome:
                async with semaphore:
                    return await run_task(client=client, challenge=challenge, config=config)

            results = await asyncio.gather(
                *(guarded(challenge) for challenge in selection), return_exceptions=True
            )
            for challenge, result in zip(selection, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    kind = platform_failure_kind(result)
                    if kind and report.platform_kind is None:
                        report.platform_kind = kind
                    report.outcomes.append(
                        TaskOutcome(
                            code=challenge.code,
                            difficulty=challenge.difficulty_label,
                            status="failed",
                            exit_reason=f"platform:{kind}" if kind else f"error:{type(result).__name__}",
                            rounds=0,
                            started=True,
                        )
                    )
                    continue
                report.outcomes.append(result)
            for outcome in report.outcomes:
                for event in client.events_for(outcome.code):
                    append_jsonl(config.runs_root / outcome.code / "platform.jsonl", event)
            append_jsonl(config.runs_root / "platform.jsonl", {"events": client.events})
    except PlatformUnusable as exc:
        report.platform_kind = exc.kind
        report.message = exc.detail or exc.kind
    except ContainerError as exc:
        # No Docker on this worker is an environment failure, not a bad command.
        report.message = str(exc)
        report.platform_kind = "docker_unavailable"
    except SpikeError as exc:
        report.message = str(exc)
        report.platform_kind = "usage_error"
    except Exception as exc:  # noqa: BLE001 - classified platform failures exit 3
        kind = platform_failure_kind(exc)
        report.platform_kind = kind or f"unhandled:{type(exc).__name__}"
        report.message = str(exc)
    report.finished_at = utc_now()
    (config.runs_root / "sweep.json").write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def plan_only(config: SweepConfig) -> list[dict[str, object]]:
    """Print the selection and the exact commands without touching the platform."""

    if not config.challenges:
        raise SpikeError("--plan-only requires explicit --challenge codes")
    plan: list[dict[str, object]] = []
    for code in config.challenges:
        layout = RunLayout(root=config.runs_root / code, code=code)
        for index, session_id in enumerate(config.session_ids, start=1):
            paths = layout.session(index=index, session_id=session_id)
            spec = session_spec(
                config=config,
                layout=layout,
                paths=paths,
                challenge=code,
                objective="<rendered from the platform at run time>",
                addresses=(),
                round_seconds=config.round_seconds,
            )
            plan.append(
                {
                    "challenge": code,
                    "session": session_id,
                    "container": spec.name,
                    "create": " ".join(redact_command(create_command(spec))),
                }
            )
    return plan


NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def container_name(challenge: str, session_id: str) -> str:
    """A Docker-legal container name for one session."""

    safe = NAME_UNSAFE.sub("-", challenge).strip("-") or "challenge"
    return f"spike-{safe}-{session_id}"


SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def redact_command(command: Sequence[str]) -> list[str]:
    """Mask any inline ``NAME=VALUE`` whose name looks like a credential."""

    redacted: list[str] = []
    for token in command:
        name, separator, _ = token.partition("=")
        if separator and any(hint in name.upper() for hint in SECRET_HINTS):
            redacted.append(f"{name}=<redacted>")
        else:
            redacted.append(token)
    return redacted
