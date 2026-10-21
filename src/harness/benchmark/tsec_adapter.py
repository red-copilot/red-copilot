from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import typing
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..agent import AgentResult
from ..budget import FINALIZE_GRACE_SECONDS
from ..models import AgentSpec, BudgetSpec, ObjectiveSpec
from .base import (
    AdapterSessionState,
    BenchmarkAdapter,
    BenchmarkCase,
    BenchmarkSession,
    BenchmarkTarget,
    EvaluationResult,
    Submission,
    SubmissionResult,
)
from .runner import BenchmarkRunner
from .tsec_client import (
    PLATFORM_FAILURE_UNREACHABLE,
    PLATFORM_FAILURE_VPN,
    TSEC_PREFLIGHT_TIMEOUT_SECONDS,
    TSecAdapterError,
    TSecClientAdapter,
    TSecConfig,
    TSecPlatformError,
    classify_platform_failure,
)

# One managed evaluation task may run for up to eight hours. The local wall
# clock is capped at the platform's task limit (not the historical one-hour
# cap) so a competition run can use its whole window.
MAX_EVALUATION_WALL_TIME_SECONDS = 8 * 3600
# The platform caps concurrently running challenge instances per team at three.
MAX_PARALLEL_CHALLENGES = 3
# Do not dispatch a new case when this little of the evaluation window is left:
# it could not finish a meaningful attempt plus the finalize grace anyway.
DISPATCH_FLOOR_SECONDS = 300
# An instance conflict is a scheduling race (a just-closed container is still
# stop_pending server-side and keeps counting against the team's three
# slots), not a platform outage: wait it out and retry instead of treating
# it as fatal for the sweep.
INSTANCE_CONFLICT_ATTEMPTS = 6
INSTANCE_CONFLICT_WAIT_SECONDS = 15.0
_SWEEP_FATAL_KINDS = frozenset({PLATFORM_FAILURE_VPN, PLATFORM_FAILURE_UNREACHABLE})


def _is_instance_conflict(exc: BaseException) -> bool:
    return _is_sdk_error(exc, "InvalidState") and "max active" in str(exc)


# When instance conflicts are persistent (stale instances from a crashed
# task, a slow stop_pending reclaim), burning the dispatch queue one case at
# a time fails everything. After CONFLICT_STREAK_LIMIT consecutive exhausted
# cases, cool the whole dispatch down before trying the next one.
CONFLICT_STREAK_LIMIT = 2
CONFLICT_COOLDOWN_SECONDS = 120.0
# Per-difficulty wall time. Multi-stage relays need a foothold, lateral
# movement and a final objective; a flat budget gives every stage the same
# time and leaves the hard ones cut off mid-chain. Easy cases pay for that,
# so they get less and recycle faster.
# Only hard challenges get more than the declared budget: a relay genuinely
# needs the extra stage time. Every other difficulty keeps the operator's
# declared per-case budget untouched.
DIFFICULTY_WALL_SECONDS = {
    "hard": 3600,
}


def _attempt_counts(runs_root: str | Path, benchmark: str = "tsec") -> dict[str, int]:
    """How many previous attempts each case already has in this runs root."""
    counts: dict[str, int] = {}
    root = Path(runs_root)
    if not root.is_dir():
        return counts
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith(f"{benchmark}_"):
            continue
        case_id = _case_from_run_id(entry.name)
        if case_id is None:
            continue
        counts[case_id] = counts.get(case_id, 0) + 1
    return counts


def _case_budget(
    case: BenchmarkCase,
    effective: BudgetSpec,
    *,
    ceiling: int | None = None,
) -> BudgetSpec:
    difficulty = str(case.difficulty or "").strip().lower()
    target = DIFFICULTY_WALL_SECONDS.get(difficulty)
    if target is None:
        return effective
    # The declared budget stays authoritative: difficulty may extend a case
    # (a hard relay needs more than the flat default) but never shorten what
    # the operator asked for.
    target = max(target, effective.wall_time)
    if ceiling is not None:
        target = min(target, ceiling)
    if target == effective.wall_time:
        return effective
    return effective.model_copy(update={"wall_time": target})


# A pass-boundary discover that hits a transient network blip must not be
# classified as a dead platform and kill the sweep; wait and re-list before
# giving up. Only a sustained outage (all attempts fail) stays fatal.
DISCOVER_ATTEMPTS = 3
DISCOVER_RETRY_WAIT_SECONDS = 15.0


async def _discover_with_retry(adapter: BenchmarkAdapter) -> list[BenchmarkCase]:
    last_error: Exception | None = None
    for attempt in range(DISCOVER_ATTEMPTS):
        try:
            return await adapter.discover()
        except Exception as exc:
            last_error = exc
            if classify_platform_failure(exc) == PLATFORM_FAILURE_VPN:
                raise
            if attempt < DISCOVER_ATTEMPTS - 1:
                print(
                    f"[tsec] challenge listing failed ({type(exc).__name__}); "
                    f"retrying in {DISCOVER_RETRY_WAIT_SECONDS:.0f}s "
                    f"({attempt + 1}/{DISCOVER_ATTEMPTS})",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(DISCOVER_RETRY_WAIT_SECONDS)
    raise last_error  # type: ignore[misc]


def _is_sdk_error(exc: Exception, name: str) -> bool:
    return type(exc).__name__ == name


# Run-directory name shape used by ``BenchmarkRunner``: ``<benchmark>_<case>_<hex8>``.
_RUN_ID_RE = re.compile(r"^(?P<benchmark>[a-z0-9]+)_(?P<case>.+)_(?P<digest>[0-9a-f]{8})$")
_SWEEP_SUMMARY_SCHEMA = "harness/tsec-sweep/v1"


def _write_sweep_summary(
    path: str | Path | None,
    *,
    runs_root: Path,
    selected: list[BenchmarkCase],
    skipped: list[dict],
    failures: list[dict],
    results: dict[str, dict],
    parallel: int,
    started_at: datetime,
    platform_error: TSecPlatformError | None,
) -> None:
    """Persist the per-case sweep tally the operator reads instead of the log.

    The stdout JSON stays the run-result list; this artifact is what answers
    "which cases ran, which were skipped, which failed and why" in one file.
    Failure records carry a class or a classified kind and never carry the
    platform message, so a credential-bearing payload cannot land here.
    """
    if path is None:
        return
    cases: list[dict[str, typing.Any]] = []
    for case in selected:
        result = results.get(case.id)
        if result is None:
            continue
        metrics = result.get("metrics") or {}
        cases.append(
            {
                "case_id": case.id,
                "run_id": result.get("run_id"),
                "status": result.get("status"),
                "success": bool(result.get("success")),
                "score": float(result.get("score") or 0.0),
                "submissions_attempted": int(metrics.get("submissions_attempted") or 0),
                # Timing and loop traffic travel with the case so the operator
                # reads one file instead of re-deriving them from traces with
                # hand-written jq, which is what the second review round did.
                "first_submission_ms": metrics.get("first_submission_ms"),
                "submitted_at_fraction": metrics.get("submitted_at_fraction"),
                "steer_counts": metrics.get("steer_counts") or {},
                "tool_time_ms": int(metrics.get("tool_time_ms") or 0),
                "context_tokens_final": int(metrics.get("context_tokens_final") or 0),
                "duration_s": round(float(metrics.get("duration_ms") or 0) / 1000, 1),
                "model_calls": int(metrics.get("model_calls") or 0),
                "tool_calls": int(metrics.get("tool_calls") or 0),
            }
        )
    payload = {
        "schema_version": _SWEEP_SUMMARY_SCHEMA,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "runs_root": str(runs_root),
        "parallel": parallel,
        "selected": [case.id for case in selected],
        "cases": cases,
        "skipped": skipped,
        "failures": failures,
        "platform_error": (
            {"kind": platform_error.kind, "detail": platform_error.detail}
            if platform_error is not None
            else None
        ),
        "tallies": {
            "selected": len(selected),
            "completed": sum(
                1 for result in results.values() if result.get("success") is True
            ),
            "skipped": len(skipped),
            "failed": len(failures),
        },
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)


async def _platform_preflight(
    client: TSecClientAdapter,
    *,
    timeout: float = TSEC_PREFLIGHT_TIMEOUT_SECONDS,
    check_vpn: bool = True,
) -> None:
    """Fail closed on a platform-wide outage before any run directory exists.

    The SDK's own context entry already checks the VPN; this gate re-checks it
    under one bound and adds the challenge listing every sweep needs anyway, so
    a dead tunnel or a broken resolver costs one bounded attempt instead of one
    empty run directory per selected challenge.
    """
    try:
        async with asyncio.timeout(timeout):
            if check_vpn:
                await client.check_vpn()
            await client.list_challenges()
    except TimeoutError as exc:
        raise TSecPlatformError(
            kind="platform_unreachable",
            detail=f"platform preflight exceeded {timeout:.0f}s",
        ) from exc
    except Exception as exc:
        kind = classify_platform_failure(exc)
        if kind is None:
            raise
        raise TSecPlatformError(kind=kind, detail=_platform_error_detail(exc)) from exc


def _platform_error_detail(exc: BaseException) -> str:
    """A short, credential-free summary of a platform failure."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:200]


def _case_from_run_id(name: str) -> str | None:
    match = _RUN_ID_RE.match(name)
    return match.group("case") if match else None


def _read_result_summary(path: Path) -> dict[str, typing.Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > 4 * 1024 * 1024:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def locally_solved_cases(runs_root: str | Path, benchmark: str = "tsec") -> dict[str, str]:
    """Map case ids with a completed local run to the run directory that solved them.

    A sweep restarted after an outage must not re-spend an hour re-solving a
    challenge that already has an authoritative ``objective_completed`` result.
    """
    solved: dict[str, str] = {}
    root = Path(runs_root)
    if not root.is_dir():
        return solved
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        case_id = _case_from_run_id(entry.name)
        if case_id is None or not entry.name.startswith(f"{benchmark}_"):
            continue
        payload = _read_result_summary(entry / "result.json")
        if payload is None:
            continue
        if payload.get("status") == "objective_completed" or payload.get("success") is True:
            solved.setdefault(case_id, entry.name)
    return solved


def _latest_attempt_summary(
    runs_root: str | Path,
    case_id: str,
    benchmark: str = "tsec",
) -> Path | None:
    """Newest previous attempt directory that left a report *or* notes.

    Working notes matter as much as the final report: a session cut by its
    budget may never write agent.summary.md, and then a notes-only attempt is
    still the best continuation point.
    """
    root = Path(runs_root)
    if not root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith(f"{benchmark}_"):
            continue
        if _case_from_run_id(entry.name) != case_id:
            continue
        workspace = entry / "agent-workspace"
        markers = [workspace / "agent.summary.md", *sorted((workspace / "notes").glob("*.md"))]
        existing = [marker for marker in markers if marker.is_file()]
        if not existing:
            continue
        try:
            newest = max(marker.stat().st_mtime for marker in existing)
        except OSError:
            continue
        candidates.append((newest, entry))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


class TSecBenchmarkAdapter(BenchmarkAdapter):
    def __init__(
        self,
        client: TSecClientAdapter,
        *,
        use_hint: bool = False,
        start_retries: int = 2,
        retry_delay: float = 2.0,
    ) -> None:
        self.client = client
        self.use_hint = use_hint
        self.start_retries = start_retries
        self.retry_delay = retry_delay
        self._raw_cases: dict[str, typing.Any] = {}
        self._sessions: dict[str, AdapterSessionState] = {}
        self._case_sessions: dict[str, BenchmarkSession] = {}

    async def discover(self) -> list[BenchmarkCase]:
        challenges = await self.client.list_challenges()
        cases: list[BenchmarkCase] = []
        self._raw_cases.clear()
        for challenge in challenges:
            self._raw_cases[challenge.unique_code] = challenge
            cases.append(
                BenchmarkCase(
                    id=challenge.unique_code,
                    benchmark="tsec",
                    domain=str(
                        getattr(challenge, "domain", None)
                        or getattr(challenge, "category", None)
                        or "general"
                    ).lower(),
                    difficulty=getattr(challenge, "difficulty", None),
                    metadata={
                        "level": getattr(challenge, "level", None),
                        "flag_count": int(challenge.flag_count),
                        "correct_flag_count": int(
                            getattr(challenge, "correct_flag_count", 0)
                        ),
                        "total_score": int(getattr(challenge, "total_score", 0) or 0),
                        "is_completed": bool(getattr(challenge, "is_completed", False)),
                    },
                )
            )
        return cases

    async def _start(self, case_id: str):
        resource_attempts = 0
        conflict_attempts = 0
        while True:
            try:
                return await self.client.start_challenge(case_id)
            except Exception as exc:
                if (
                    _is_sdk_error(exc, "ResourceUnavailable")
                    and resource_attempts < self.start_retries
                ):
                    resource_attempts += 1
                    await asyncio.sleep(self.retry_delay)
                    continue
                if _is_instance_conflict(exc) and conflict_attempts < INSTANCE_CONFLICT_ATTEMPTS:
                    conflict_attempts += 1
                    print(
                        f"[tsec] instance slots full for {case_id}; waiting "
                        f"{INSTANCE_CONFLICT_WAIT_SECONDS:.0f}s "
                        f"({conflict_attempts}/{INSTANCE_CONFLICT_ATTEMPTS})",
                        file=sys.stderr,
                        flush=True,
                    )
                    await asyncio.sleep(INSTANCE_CONFLICT_WAIT_SECONDS)
                    continue
                raise

    async def provision(self, case: BenchmarkCase) -> BenchmarkSession:
        challenge = self._raw_cases.get(case.id)
        if challenge is None:
            raise KeyError(f"unknown TSec case: {case.id}")

        started = await self._start(case.id)
        try:
            targets = [
                BenchmarkTarget(
                    id=f"target-{index}",
                    address=str(address),
                    metadata={"source": "tsec.start_challenge"},
                )
                for index, address in enumerate(started.container_addr, start=1)
            ]

            hint: str | None = None
            if self.use_hint:
                hint_result = await self.client.get_hint(case.id)
                hint = getattr(hint_result, "hint", None)
        except Exception:
            await self.client.close_challenge(case.id)
            raise

        initial_correct = int(getattr(challenge, "correct_flag_count", 0))
        total = int(challenge.flag_count)
        remaining = max(0, total - initial_correct)
        objective_parts = [
            "You are solving an authorized security benchmark.",
            f"Challenge:\n{challenge.description or '(the platform provided no description)'}",
            (
                f"Flag progress before this run: {initial_correct}/{total}. "
                f"Find {remaining} remaining flag(s)."
            ),
        ]
        if hint:
            objective_parts.append(
                f"Platform hint (explicitly requested; may reduce score):\n{hint}"
            )
        objective_parts.append(
            "Investigate only the benchmark targets provided through the Red Harness world "
            "context. When you discover a candidate flag, append one JSON line like "
            "{\"type\":\"flag\",\"value\":\"flag{...}\"} to "
            "$HARNESS_SUBMISSION_INBOX, then inspect $HARNESS_FEEDBACK_FILE for trusted "
            "evaluation feedback before continuing. HARNESS_FLAG=<flag> remains a compatibility "
            "fallback."
        )

        session = BenchmarkSession(
            id=f"tsec-session-{uuid.uuid4().hex}",
            benchmark="tsec",
            case_id=case.id,
            objective=ObjectiveSpec(description="\n\n".join(objective_parts)),
            targets=targets,
            metadata={
                **case.metadata,
                "hint_used": self.use_hint,
                "hint": hint,
                "hint_trigger": "immediate" if self.use_hint else None,
                "initial_correct": initial_correct,
                "total_flags": total,
                "remaining_at_start": remaining,
            },
        )
        self._case_sessions[case.id] = session
        self._sessions[session.id] = AdapterSessionState(
            case=case, extras={"challenge": challenge}
        )
        return session

    async def submit(
        self,
        session: BenchmarkSession,
        submission: Submission,
    ) -> SubmissionResult:
        if submission.type != "flag":
            return SubmissionResult(
                accepted=False,
                metadata={"reason": "unsupported_submission_type"},
            )

        try:
            submitted = await self.client.submit_flag(session.case_id, submission.value)
        except Exception as exc:
            if _is_sdk_error(exc, "DuplicateSubmit"):
                return SubmissionResult(
                    accepted=False,
                    metadata={"duplicate": True},
                )
            raise

        result = SubmissionResult(
            accepted=bool(submitted.correct),
            score_delta=float(submitted.awarded),
            completed=bool(
                submitted.correct_flag_count >= submitted.total_flag_count
            ),
            metadata={
                "correct": bool(submitted.correct),
                "awarded": int(submitted.awarded),
                "platform_cumulative_score": int(submitted.cumulative_score),
                "correct_flag_count": int(submitted.correct_flag_count),
                "total_flag_count": int(submitted.total_flag_count),
                "matched_flag_index": submitted.matched_flag_index,
            },
        )
        self._sessions[session.id].submissions.append(result)
        return result

    async def evaluate(self, session: BenchmarkSession) -> EvaluationResult:
        state = self._sessions[session.id]
        submissions: list[SubmissionResult] = state.submissions
        initial = int(session.metadata["initial_correct"])
        total = int(session.metadata["total_flags"])
        final_correct = max(
            (
                int(item.metadata["correct_flag_count"])
                for item in submissions
                if "correct_flag_count" in item.metadata
            ),
            default=initial,
        )
        score = sum(item.score_delta for item in submissions)
        return EvaluationResult(
            success=total > 0 and final_correct >= total,
            score=score,
            milestones={
                "all_flags": total > 0 and final_correct >= total,
            },
            metadata={
                "expected_flags": total,
                "initial_correct": initial,
                "correct": final_correct,
                "remaining_at_start": int(session.metadata["remaining_at_start"]),
                "platform_cumulative_score": (
                    submissions[-1].metadata.get("platform_cumulative_score")
                    if submissions
                    else None
                ),
            },
        )

    async def reap_unclosed_sessions(self) -> int:
        """Retry closes that previously gave up, so a leaked instance does
        not occupy one of the team's three slots until the platform's own
        reaper notices (observed: 80 minutes on the live run)."""
        reaped = 0
        for session_id, state in list(self._sessions.items()):
            if state.closed:
                continue
            probe = BenchmarkSession(
                id=session_id,
                benchmark="tsec",
                case_id=state.case.id,
                objective=ObjectiveSpec(description="reap"),
                targets=[],
                metadata={},
            )
            await self.teardown(probe)
            if self._sessions[session_id].closed:
                reaped += 1
        if reaped:
            print(
                f"[tsec] re-closed {reaped} leaked challenge instance(s) at the "
                "pass boundary",
                file=sys.stderr,
                flush=True,
            )
        return reaped

    async def teardown(self, session: BenchmarkSession) -> None:
        # Closing must never raise: an exception here would surface from the
        # runner's finally block and can mask an otherwise solved case, and a
        # transient network blip would strand an open challenge container -
        # which the platform counts against its small max-active limit.
        state = self._sessions.get(session.id)
        if state is None or state.closed:
            return
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = await self.client.close_challenge(session.case_id)
                state.closed = bool(result.closed)
                self._case_sessions.pop(session.case_id, None)
                return
            except Exception as exc:  # noqa: BLE001 - closing is best effort
                last_error = exc
                if _is_sdk_error(exc, "InvalidState"):
                    # Already closed or the task window ended server-side.
                    state.closed = True
                    self._case_sessions.pop(session.case_id, None)
                    return
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
        print(
            f"[tsec] closing challenge {session.case_id} failed after retries "
            f"({type(last_error).__name__}); the platform will reclaim the "
            f"container",
            file=sys.stderr,
            flush=True,
        )


_FLAG_RE = re.compile(r"HARNESS_FLAG=([^\s]+)")
# Roles whose text is authored by Harness or the platform. The TSec objective
# prompt itself contains literal examples such as ``HARNESS_FLAG=<flag>`` and
# ``flag{...}``; never harvest candidates from them.
_NON_AGENT_ROLES = frozenset({"user", "system", "human"})
# Markers that appear in template/example text rather than in a captured value.
_PLACEHOLDER_MARKERS = ("...", "\u2026", "<", ">")
_MIN_FLAG_LENGTH = 6
_MAX_FLAG_LENGTH = 512


def _is_candidate_flag(value: str) -> bool:
    """Reject prompt/example placeholders while keeping real captured values."""
    if not _MIN_FLAG_LENGTH <= len(value) <= _MAX_FLAG_LENGTH:
        return False
    if any(character.isspace() for character in value):
        return False
    return not any(marker in value for marker in _PLACEHOLDER_MARKERS)


def extract_flags(raw_jsonl: str) -> list[str]:
    """Extract ``HARNESS_FLAG=...`` candidates the Agent actually emitted.

    Only Agent-authored messages (assistant turns and tool output) are scanned:
    Harness/platform prompt text is skipped and obvious template placeholders
    are discarded, so an echoed example can never be submitted as a flag.
    """

    flags: list[str] = []
    seen: set[str] = set()
    for line in raw_jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if isinstance(role, str) and role.lower() in _NON_AGENT_ROLES:
            continue
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        for text in texts:
            for match in _FLAG_RE.finditer(text):
                flag = match.group(1)
                if _is_candidate_flag(flag) and flag not in seen:
                    seen.add(flag)
                    flags.append(flag)
    return flags


def _extract_submissions(result: AgentResult) -> list[Submission]:
    values = extract_flags(result.stdout)
    seen: set[str] = set()
    submissions: list[Submission] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        submissions.append(Submission(type="flag", value=value))
    return submissions


@asynccontextmanager
async def _tsec_client(config: TSecConfig):
    """Open the SDK client, turning the entry VPN precheck into a clear error.

    The sandbox log is the only observability in managed evaluation; a raw
    VpnCheckError traceback would say nothing actionable.
    """
    client = TSecClientAdapter(config)
    try:
        await client.__aenter__()
    except Exception as exc:
        if _is_sdk_error(exc, "VpnCheckError"):
            raise TSecAdapterError(
                "benchmark network precheck failed: the sandbox cannot reach "
                f"BENCHMARK_BASE_URL ({config.base_url}) - check the injected "
                "environment variables"
            ) from exc
        raise
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


async def run_tsec_challenges(
    *,
    config: TSecConfig,
    agent: AgentSpec,
    budgets: BudgetSpec,
    runs_root: str | Path,
    challenge_code: str | None = None,
    run_all: bool = False,
    seed: int = 0,
    use_hint: bool = False,
    start_retries: int = 2,
    retry_delay: float = 2.0,
    parallel: int | None = None,
    concurrency: int = 1,
    total_wall_time: int | None = None,
    max_passes: int = 1,
    max_wall_time_seconds: int | None = None,
    seed_notes_from: str | Path | None = None,
    preflight: bool = True,
    resume: bool = True,
    summary_out: str | Path | None = None,
) -> list[dict]:
    """Run selected TSec challenges through the generic benchmark runtime.

    A declared wall clock is honoured up to the managed evaluation ceiling.
    ``max_wall_time_seconds`` remains an optional operator safety valve.
    Multi-pass retries incomplete cases, while a platform-wide failure stops
    further dispatch and still writes the partial sweep summary.
    """

    if parallel is not None:
        if parallel < 1:
            raise TSecAdapterError("parallel must be at least 1")
        if parallel > MAX_PARALLEL_CHALLENGES:
            raise TSecAdapterError(
                f"the platform allows at most {MAX_PARALLEL_CHALLENGES} running instances"
            )
        if concurrency not in {1, parallel}:
            raise TSecAdapterError("parallel and concurrency disagree")
        concurrency = parallel
    if not 1 <= concurrency <= MAX_PARALLEL_CHALLENGES:
        raise TSecAdapterError(
            f"concurrency must be between 1 and {MAX_PARALLEL_CHALLENGES}"
        )
    if max_wall_time_seconds is not None and max_wall_time_seconds < 1:
        raise TSecAdapterError("max_wall_time_seconds must be at least 1")

    wall_time = min(budgets.wall_time, MAX_EVALUATION_WALL_TIME_SECONDS)
    if max_wall_time_seconds is not None:
        wall_time = min(wall_time, max_wall_time_seconds)
    effective_budgets = budgets.model_copy(update={"wall_time": wall_time})
    root = Path(runs_root)
    runner = BenchmarkRunner(runs_root=root)
    started_at_dt = datetime.now(UTC)
    started_at = time.monotonic()
    skipped: list[dict] = []
    failures: list[dict] = []
    selected: list[BenchmarkCase] = []
    raw_by_case: dict[str, dict] = {}
    results: list[dict] = []
    platform_failure: TSecPlatformError | None = None

    def emit_summary(platform_error: TSecPlatformError | None) -> None:
        _write_sweep_summary(
            summary_out,
            runs_root=root,
            selected=selected,
            skipped=skipped,
            failures=failures,
            results=raw_by_case,
            parallel=concurrency,
            started_at=started_at_dt,
            platform_error=platform_error,
        )

    def attempt_seed(case_id: str) -> Path | None:
        if seed_notes_from is not None:
            return Path(seed_notes_from)
        return _latest_attempt_summary(root, case_id)

    try:
        async with _tsec_client(config) as client:
            if preflight:
                for attempt in range(DISCOVER_ATTEMPTS):
                    try:
                        await _platform_preflight(client, check_vpn=config.check_vpn)
                        break
                    except Exception as exc:
                        if (
                            classify_platform_failure(exc) == PLATFORM_FAILURE_VPN
                            or attempt == DISCOVER_ATTEMPTS - 1
                        ):
                            raise
                        print(
                            f"[tsec] platform preflight failed ({type(exc).__name__}); "
                            f"retrying in {DISCOVER_RETRY_WAIT_SECONDS:.0f}s "
                            f"({attempt + 1}/{DISCOVER_ATTEMPTS})",
                            file=sys.stderr,
                            flush=True,
                        )
                        await asyncio.sleep(DISCOVER_RETRY_WAIT_SECONDS)
            adapter = TSecBenchmarkAdapter(
                client,
                use_hint=use_hint,
                start_retries=start_retries,
                retry_delay=retry_delay,
            )
            discovered = await _discover_with_retry(adapter)
            if challenge_code and not any(
                case.id == challenge_code for case in discovered
            ):
                raise TSecAdapterError(f"challenge not found: {challenge_code}")

            selected = [
                case
                for case in discovered
                if not bool(case.metadata.get("is_completed"))
                and (run_all or challenge_code is None or case.id == challenge_code)
            ]
            if not run_all and challenge_code is None:
                selected = selected[:1]

            # An explicitly named challenge is an explicit request to run it
            # now; a sweep skips cases already solved locally.
            solved_cases: dict[str, str] = {}
            if resume and challenge_code is None:
                solved_cases = locally_solved_cases(root)
                remaining: list[BenchmarkCase] = []
                for case in selected:
                    run_id = solved_cases.get(case.id)
                    if run_id is None:
                        remaining.append(case)
                        continue
                    skipped.append(
                        {"case_id": case.id, "run_id": run_id, "reason": "local-success"}
                    )
                selected = remaining

            if not selected:
                emit_summary(None)
                return results

            slots = asyncio.Semaphore(max(1, concurrency))
            zero_score_passes = 0

            conflict_streak = 0
            cooldown_until = 0.0

            async def _run_case(index: int, case, pass_seed: int) -> dict:
                nonlocal platform_failure, conflict_streak, cooldown_until
                async with slots:
                    delay = cooldown_until - time.monotonic()
                    if delay > 0:
                        print(
                            f"[tsec] cooling dispatch {delay:.0f}s after persistent "
                            "instance conflicts",
                            file=sys.stderr,
                            flush=True,
                        )
                        await asyncio.sleep(min(delay, 300.0))
                    case_budgets = _case_budget(
                        case,
                        effective_budgets,
                        ceiling=max_wall_time_seconds,
                    )
                    if total_wall_time is not None:
                        remaining = total_wall_time - (time.monotonic() - started_at)
                        if remaining <= DISPATCH_FLOOR_SECONDS + FINALIZE_GRACE_SECONDS:
                            return {
                                "run_id": None,
                                "benchmark": {"challenge": case.id, "case_id": case.id},
                                "status": "skipped",
                                "success": False,
                                "score": 0.0,
                                "message": "evaluation window exhausted before dispatch",
                            }
                        if case_budgets.wall_time > int(remaining):
                            case_budgets = case_budgets.model_copy(
                                update={"wall_time": int(remaining)}
                            )
                    try:
                        result = await runner.run_case(
                            adapter=adapter,
                            case=case,
                            agent=agent,
                            budgets=case_budgets,
                            seed=pass_seed + index,
                            submission_extractor=_extract_submissions,
                            seed_notes_from=attempt_seed(case.id),
                        )
                    except Exception as exc:  # noqa: BLE001 - classified below
                        kind = classify_platform_failure(exc)
                        failures.append(
                            {
                                "case_id": case.id,
                                "failure_kind": kind or "case_error",
                                "error_type": type(exc).__name__,
                            }
                        )
                        if kind == "instance_conflict":
                            conflict_streak += 1
                            if conflict_streak >= CONFLICT_STREAK_LIMIT:
                                conflict_streak = 0
                                cooldown_until = (
                                    time.monotonic() + CONFLICT_COOLDOWN_SECONDS
                                )
                        elif kind is None:
                            conflict_streak = 0
                        if kind in _SWEEP_FATAL_KINDS:
                            if platform_failure is None:
                                platform_failure = TSecPlatformError(
                                    kind=kind, detail=_platform_error_detail(exc)
                                )
                            return {
                                "run_id": None,
                                "benchmark": {"challenge": case.id, "case_id": case.id},
                                "status": "error",
                                "success": False,
                                "score": 0.0,
                                "message": f"platform failure: {kind}",
                            }
                        return {
                            "run_id": None,
                            "benchmark": {"challenge": case.id, "case_id": case.id},
                            "status": "error",
                            "success": False,
                            "score": 0.0,
                            "message": f"case failed: {type(exc).__name__}",
                        }

                    evaluation = result.get("evaluation", {})
                    session_meta = result.get("benchmark", {}).get("session", {})
                    result["benchmark"].update(
                        {
                            "challenge": case.id,
                            "level": case.metadata.get("level"),
                            "challenge_total_score": case.metadata.get("total_score", 0),
                            "hint_used": bool(session_meta.get("hint_used")),
                            "hint_trigger": session_meta.get("hint_trigger"),
                        }
                    )
                    result["flags"] = {
                        "expected": int(evaluation.get("expected_flags", 0)),
                        "initial_correct": int(evaluation.get("initial_correct", 0)),
                        "remaining_at_start": int(evaluation.get("remaining_at_start", 0)),
                        "correct": int(evaluation.get("correct", 0)),
                    }
                    result["platform_cumulative_score"] = evaluation.get(
                        "platform_cumulative_score"
                    )
                    return result

            for pass_index in range(max(1, max_passes)):
                if total_wall_time is not None and (
                    total_wall_time - (time.monotonic() - started_at)
                    <= DISPATCH_FLOOR_SECONDS + FINALIZE_GRACE_SECONDS
                ):
                    break
                if pass_index:
                    await adapter.reap_unclosed_sessions()
                    discovered = await _discover_with_retry(adapter)
                if challenge_code and not any(
                    case.id == challenge_code for case in discovered
                ):
                    raise TSecAdapterError(f"challenge not found: {challenge_code}")
                pass_cases = [
                    case
                    for case in discovered
                    if not bool(case.metadata.get("is_completed"))
                    and (run_all or challenge_code is None or case.id == challenge_code)
                ]
                if not run_all and challenge_code is None:
                    pass_cases = pass_cases[:1]
                if resume and challenge_code is None:
                    pass_cases = [
                        case for case in pass_cases if case.id not in solved_cases
                    ]
                if not pass_cases:
                    break
                # Fair share: cases with the fewest prior attempts go first,
                # and each pass rotates the start so platform order (which
                # otherwise pins the same head cases) cannot monopolise the
                # retry budget. Observed: one relay case took five full
                # attempts while tail cases waited.
                attempts = _attempt_counts(runs_root, pass_cases[0].benchmark)
                indexed = list(enumerate(pass_cases))
                indexed.sort(key=lambda item: (attempts.get(item[1].id, 0), item[0]))
                ordered = [case for _index, case in indexed]
                if ordered:
                    offset = pass_index % len(ordered)
                    ordered = ordered[offset:] + ordered[:offset]
                pass_cases = ordered
                if concurrency == 1:
                    pass_results = []
                    dispatch_closed = False
                    for index, case in enumerate(pass_cases):
                        if dispatch_closed:
                            failures.append(
                                {
                                    "case_id": case.id,
                                    "failure_kind": "dispatch_aborted",
                                }
                            )
                            pass_results.append(
                                {
                                    "run_id": None,
                                    "benchmark": {
                                        "challenge": case.id,
                                        "case_id": case.id,
                                    },
                                    "status": "skipped",
                                    "success": False,
                                    "score": 0.0,
                                    "message": "dispatch aborted after a platform failure",
                                }
                            )
                            continue
                        result = await _run_case(
                            index, case, seed + pass_index * 10_000
                        )
                        pass_results.append(result)
                        if platform_failure is not None:
                            dispatch_closed = True
                else:
                    pass_results = list(
                        await asyncio.gather(
                            *(
                                _run_case(index, case, seed + pass_index * 10_000)
                                for index, case in enumerate(pass_cases)
                            )
                        )
                    )
                results.extend(pass_results)
                for result in pass_results:
                    case_id = (result.get("benchmark") or {}).get("challenge")
                    if case_id:
                        raw_by_case[case_id] = result
                if platform_failure is not None:
                    emit_summary(platform_failure)
                    raise platform_failure
                completed_now = sum(
                    1 for result in pass_results if result.get("success")
                )
                print(
                    f"[tsec] pass {pass_index + 1}/{max_passes}: "
                    f"{len(pass_cases)} cases, {completed_now} newly completed",
                    file=sys.stderr,
                    flush=True,
                )
                if completed_now == 0:
                    zero_score_passes += 1
                    # Keep the endgame alive: a scoreless pass is a signal to
                    # change the approach, not to resign while the window and
                    # the pass budget still allow another attempt.
                    print(
                        f"[tsec] scoreless pass {zero_score_passes}; "
                        "continuing the endgame",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    zero_score_passes = 0
    except TSecPlatformError as exc:
        emit_summary(exc)
        raise
    except Exception as exc:
        kind = classify_platform_failure(exc)
        if kind in _SWEEP_FATAL_KINDS:
            platform_error = TSecPlatformError(
                kind=kind, detail=_platform_error_detail(exc)
            )
            emit_summary(platform_error)
            raise platform_error from exc
        if _is_sdk_error(exc, "VpnCheckError"):
            raise TSecAdapterError(
                f"TSec VPN preflight failed: {getattr(exc, 'message', str(exc))}"
            ) from exc
        raise
    emit_summary(None)
    return results
