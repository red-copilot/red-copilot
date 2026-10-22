from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.benchmark import tsec_adapter
from harness.benchmark.base import (
    AdapterSessionState,
    BenchmarkCase,
    BenchmarkSession,
    Submission,
)
from harness.benchmark.tsec_adapter import (
    MAX_PARALLEL_CHALLENGES,
    TSecBenchmarkAdapter,
    extract_flags,
    run_tsec_challenges,
)
from harness.benchmark.tsec_client import (
    TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS,
    TSecAdapterError,
    TSecClientAdapter,
    TSecConfig,
    TSecPlatformError,
    _TokenBucket,
    classify_platform_failure,
)
from harness.models import AgentSpec, BudgetSpec, load_agent, load_task
from harness.preflight import run_preflight


class FakeTSecClient:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.submitted: list[tuple[str, str]] = []

    async def list_challenges(self):
        return [
            SimpleNamespace(
                unique_code="WEB-001",
                description="Find the flag",
                category="web",
                difficulty="easy",
                level="L1",
                flag_count=1,
                correct_flag_count=0,
                total_score=100,
                is_completed=False,
            )
        ]

    async def start_challenge(self, unique_code: str):
        assert unique_code == "WEB-001"
        return SimpleNamespace(container_addr=["10.0.0.10:8080"])

    async def get_hint(self, unique_code: str):
        return SimpleNamespace(hint="look at the service")

    async def submit_flag(self, unique_code: str, flag: str):
        self.submitted.append((unique_code, flag))
        return SimpleNamespace(
            correct=True,
            awarded=100,
            cumulative_score=100,
            correct_flag_count=1,
            total_flag_count=1,
            matched_flag_index=0,
        )

    async def close_challenge(self, unique_code: str):
        self.closed.append(unique_code)
        return SimpleNamespace(closed=True)


def test_tsec_adapter_maps_sdk_to_generic_contracts() -> None:
    async def scenario() -> None:
        client = FakeTSecClient()
        adapter = TSecBenchmarkAdapter(client, use_hint=True)

        cases = await adapter.discover()
        assert cases == [
            BenchmarkCase(
                id="WEB-001",
                benchmark="tsec",
                domain="web",
                difficulty="easy",
                metadata={
                    "level": "L1",
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "total_score": 100,
                    "is_completed": False,
                },
            )
        ]

        session = await adapter.provision(cases[0])
        assert session.case_id == "WEB-001"
        assert session.targets[0].address == "10.0.0.10:8080"
        assert session.metadata["hint_used"] is True
        assert "look at the service" in session.objective.description

        submitted = await adapter.submit(
            session,
            Submission(type="flag", value="flag{ok}"),
        )
        assert submitted.accepted is True
        assert submitted.completed is True
        assert submitted.score_delta == 100

        evaluation = await adapter.evaluate(session)
        assert evaluation.success is True
        assert evaluation.score == 100
        assert evaluation.metadata["correct"] == 1

        await adapter.teardown(session)
        assert client.closed == ["WEB-001"]

    asyncio.run(scenario())


def test_tsec_adapter_rejects_non_flag_submission() -> None:
    async def scenario() -> None:
        client = FakeTSecClient()
        adapter = TSecBenchmarkAdapter(client)
        case = (await adapter.discover())[0]
        session = await adapter.provision(case)

        result = await adapter.submit(
            session,
            Submission(type="finding", value="not-a-flag"),
        )
        assert result.accepted is False
        assert result.metadata["reason"] == "unsupported_submission_type"

        await adapter.teardown(session)

    asyncio.run(scenario())


def test_tsec_provision_failure_closes_started_challenge() -> None:
    async def scenario() -> None:
        client = FakeTSecClient()

        async def unavailable_hint(_unique_code: str):
            raise RuntimeError("hint service unavailable")

        client.get_hint = unavailable_hint
        adapter = TSecBenchmarkAdapter(client, use_hint=True)
        case = (await adapter.discover())[0]
        try:
            await adapter.provision(case)
        except RuntimeError as exc:
            assert "hint service unavailable" in str(exc)
        else:
            raise AssertionError("provision should propagate hint failure")
        assert client.closed == ["WEB-001"]

    asyncio.run(scenario())


def test_tsec_teardown_is_idempotent() -> None:
    async def scenario() -> None:
        client = FakeTSecClient()
        adapter = TSecBenchmarkAdapter(client)
        case = (await adapter.discover())[0]
        session = await adapter.provision(case)
        await adapter.teardown(session)
        await adapter.teardown(session)
        assert client.closed == ["WEB-001"]

    asyncio.run(scenario())


def test_tsec_provision_retries_temporary_unavailable_service() -> None:
    class ResourceUnavailable(Exception):
        pass

    async def scenario() -> None:
        client = FakeTSecClient()
        starts = 0

        async def start_challenge(_unique_code: str):
            nonlocal starts
            starts += 1
            if starts == 1:
                raise ResourceUnavailable("temporarily unavailable")
            return SimpleNamespace(container_addr=["10.0.0.10:8080"])

        client.start_challenge = start_challenge
        adapter = TSecBenchmarkAdapter(client, start_retries=1, retry_delay=0)
        case = (await adapter.discover())[0]
        session = await adapter.provision(case)
        await adapter.teardown(session)
        assert starts == 2

    asyncio.run(scenario())


def test_tsec_duplicate_submission_response_is_safe() -> None:
    class DuplicateSubmit(Exception):
        pass

    async def scenario() -> None:
        client = FakeTSecClient()

        async def duplicate_submit(_unique_code: str, _flag: str):
            raise DuplicateSubmit("already submitted")

        client.submit_flag = duplicate_submit
        adapter = TSecBenchmarkAdapter(client)
        case = (await adapter.discover())[0]
        session = await adapter.provision(case)
        result = await adapter.submit(session, Submission(type="flag", value="flag{same}"))
        assert result.accepted is False
        assert result.metadata["duplicate"] is True
        await adapter.teardown(session)

    asyncio.run(scenario())


def test_tsec_malformed_start_response_fails_closed_and_cleans_up() -> None:
    async def scenario() -> None:
        client = FakeTSecClient()

        async def malformed_start(_unique_code: str):
            return SimpleNamespace()

        client.start_challenge = malformed_start
        adapter = TSecBenchmarkAdapter(client)
        case = (await adapter.discover())[0]
        try:
            await adapter.provision(case)
        except AttributeError:
            pass
        else:
            raise AssertionError("malformed service response must fail")
        assert client.closed == ["WEB-001"]

    asyncio.run(scenario())


def test_extract_flags_from_pi_jsonl() -> None:
    events = [
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "HARNESS_FLAG=flag{one}"}],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": "HARNESS_FLAG=flag{two} HARNESS_FLAG=flag{one}",
            },
        },
    ]
    raw = "\n".join(json.dumps(item) for item in events)
    assert extract_flags(raw) == ["flag{one}", "flag{two}"]


_TSEC_OBJECTIVE = (
    "Investigating only the benchmark targets: append one JSON line like "
    '{"type":"flag","value":"flag{...}"} to $HARNESS_SUBMISSION_INBOX. '
    "HARNESS_FLAG=<flag> remains a compatibility fallback."
)


def _message(role: str, text: str) -> dict:
    return {
        "type": "message_end",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def test_extract_flags_ignores_prompt_placeholders() -> None:
    # Regression: the objective prompt itself was harvested as a candidate flag,
    # producing junk submissions (e.g. "<flag>" and "flag{...}").
    raw = json.dumps(_message("user", _TSEC_OBJECTIVE))
    assert extract_flags(raw) == []
    submissions = tsec_adapter._extract_submissions(SimpleNamespace(stdout=raw))
    assert submissions == []


def test_extract_flags_keeps_agent_emitted_values_only() -> None:
    raw = "\n".join(
        json.dumps(item)
        for item in [
            _message("user", _TSEC_OBJECTIVE),
            _message("assistant", "The captured value is HARNESS_FLAG=flag{one}"),
            _message(
                "toolResult",
                "cat /var/www/flag.txt -> HARNESS_FLAG=flag{two_real_value}",
            ),
        ]
    )
    assert extract_flags(raw) == ["flag{one}", "flag{two_real_value}"]


def test_extract_flags_rejects_placeholder_shaped_values() -> None:
    # Template markers and too-short remnants are rejected; a short-but-real
    # value like "flag{x}" is deliberately still accepted (no false negatives).
    values = ["<flag>", "flag{...}", "flag{ spaced }", "fl"]
    raw = "\n".join(json.dumps(_message("assistant", f"HARNESS_FLAG={v}")) for v in values)
    assert extract_flags(raw) == []


def test_extract_submissions_does_not_raw_scan_stdout() -> None:
    # A non-JSON line must not be harvested anymore: only structured,
    # Agent-authored candidates are accepted.
    result = SimpleNamespace(stdout="not json HARNESS_FLAG=flag{rawscanvalue}")
    assert tsec_adapter._extract_submissions(result) == []


def _runner_agent() -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "harness/v1",
            "id": "pi-tsec",
            "type": "pi",
            "image": "pi:test",
            "network": "host",
            "pi": {"provider": "fake", "model": "m"},
        }
    )


class FakeRunnerClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def check_vpn(self):
        return SimpleNamespace(ok=True)

    async def list_challenges(self):
        return [
            SimpleNamespace(
                unique_code="WEB-001",
                description="Authorized web challenge",
                category="web",
                flag_count=2,
                correct_flag_count=0,
                difficulty="medium",
                level="L2",
                total_score=100,
                is_completed=False,
            )
        ]

    async def start_challenge(self, code):
        assert code == "WEB-001"
        return SimpleNamespace(container_addr=["10.0.0.2:8080"])

    async def close_challenge(self, code):
        assert code == "WEB-001"
        self.closed = True
        return SimpleNamespace(closed=True)


class FakeBenchmarkRunner:
    last_budgets: BudgetSpec | None = None
    last_kwargs: dict | None = None

    def __init__(self, *, runs_root: Path):
        self.runs_root = runs_root

    async def run_case(self, *, case, budgets, **kwargs):
        type(self).last_budgets = budgets
        type(self).last_kwargs = kwargs
        return {
            "run_id": "tsec_WEB-001_fake",
            "benchmark": {
                "type": "tsec",
                "case_id": case.id,
                "domain": case.domain,
                "difficulty": case.difficulty,
                "session": {
                    "hint_used": False,
                    "initial_correct": 0,
                    "total_flags": 2,
                    "remaining_at_start": 2,
                },
            },
            "status": "finished",
            "success": True,
            "score": 100.0,
            "evaluation": {
                "expected_flags": 2,
                "initial_correct": 0,
                "remaining_at_start": 2,
                "correct": 2,
                "platform_cumulative_score": 900,
            },
        }


def test_run_tsec_challenges_uses_generic_benchmark_contract(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            seed=1,
            max_unconfirmed_progress=200,
        )
    )

    assert len(results) == 1
    result = results[0]
    assert result["success"] is True
    assert result["score"] == 100.0
    assert result["platform_cumulative_score"] == 900
    assert result["flags"] == {
        "expected": 2,
        "initial_correct": 0,
        "remaining_at_start": 2,
        "correct": 2,
    }
    assert result["benchmark"]["challenge"] == "WEB-001"
    assert result["benchmark"]["level"] == "L2"
    assert FakeBenchmarkRunner.last_kwargs is not None
    assert FakeBenchmarkRunner.last_kwargs["max_unconfirmed_progress"] == 200


def test_run_tsec_challenges_allows_the_full_evaluation_window(
    monkeypatch, tmp_path: Path
) -> None:
    from harness.benchmark.tsec_adapter import MAX_EVALUATION_WALL_TIME_SECONDS

    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=7200),
            runs_root=tmp_path,
            challenge_code="WEB-001",
        )
    )

    # A declared budget below the platform ceiling passes through untouched...
    assert FakeBenchmarkRunner.last_budgets is not None
    assert FakeBenchmarkRunner.last_budgets.wall_time == 7200

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=999_999),
            runs_root=tmp_path,
            challenge_code="WEB-001",
        )
    )

    # ...and an oversized declaration is clamped to the 8-hour task limit.
    assert FakeBenchmarkRunner.last_budgets.wall_time == MAX_EVALUATION_WALL_TIME_SECONDS
    assert MAX_EVALUATION_WALL_TIME_SECONDS == 8 * 3600


def test_tsec_cli_defaults_to_the_full_evaluation_window() -> None:
    """The competition path must not depend on remembering --wall-time."""
    import inspect

    from harness.benchmark.tsec_adapter import MAX_EVALUATION_WALL_TIME_SECONDS
    from harness.cli import tsec

    defaults = {
        parameter.name: parameter.default
        for parameter in inspect.signature(tsec).parameters.values()
    }
    assert defaults["wall_time"] == MAX_EVALUATION_WALL_TIME_SECONDS == 8 * 3600


class _ConcurrencyProbeRunner:
    """Records how many run_case coroutines are in flight at once."""

    calls: ClassVar[list[str]] = []
    in_flight: ClassVar[int] = 0
    max_in_flight: ClassVar[int] = 0
    fail_cases: ClassVar[set[str]] = set()

    def __init__(self, *, runs_root):
        pass

    async def run_case(self, *, case, budgets, **kwargs):
        type(self).calls.append(case.id)
        type(self).in_flight += 1
        type(self).max_in_flight = max(type(self).max_in_flight, type(self).in_flight)
        try:
            await asyncio.sleep(0.05)
            if case.id in type(self).fail_cases:
                raise RuntimeError("simulated case failure")
            return {
                "run_id": f"run-{case.id}",
                "benchmark": {"case_id": case.id, "session": {}},
                "status": "finished",
                "success": True,
                "score": 1.0,
                "evaluation": {},
            }
        finally:
            type(self).in_flight -= 1


class _MultiCaseClient(FakeRunnerClient):
    async def list_challenges(self):
        return [
            SimpleNamespace(
                unique_code=f"WEB-{index:03d}",
                description=f"challenge {index}",
                category="web",
                flag_count=1,
                correct_flag_count=0,
                difficulty="easy",
                level="L1",
                total_score=10,
                is_completed=False,
            )
            for index in range(1, 4)
        ]

    async def start_challenge(self, code):
        return SimpleNamespace(container_addr=["10.0.0.2:8080"])

    async def close_challenge(self, code):
        return SimpleNamespace(closed=True)


def _reset_probe() -> None:
    _ConcurrencyProbeRunner.calls = []
    _ConcurrencyProbeRunner.in_flight = 0
    _ConcurrencyProbeRunner.max_in_flight = 0
    _ConcurrencyProbeRunner.fail_cases = set()
    _RetryProbeRunner.always_fail = False


def test_run_tsec_challenges_solves_cases_concurrently(monkeypatch, tmp_path: Path) -> None:
    _reset_probe()
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=3,
        )
    )

    assert len(results) == 3
    assert _ConcurrencyProbeRunner.max_in_flight >= 2


def test_run_tsec_challenges_serial_when_concurrency_one(monkeypatch, tmp_path: Path) -> None:
    _reset_probe()
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=1,
        )
    )

    assert _ConcurrencyProbeRunner.max_in_flight == 1


def test_run_tsec_challenges_continues_after_a_case_fails(monkeypatch, tmp_path: Path) -> None:
    _reset_probe()
    _ConcurrencyProbeRunner.fail_cases = {"WEB-001"}
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=3,
        )
    )

    by_challenge = {result["benchmark"]["challenge"]: result for result in results}
    assert by_challenge["WEB-001"]["status"] == "error"
    assert by_challenge["WEB-002"]["status"] == "finished"
    assert by_challenge["WEB-003"]["status"] == "finished"


def test_run_tsec_challenges_skips_dispatch_past_total_window(monkeypatch, tmp_path: Path) -> None:
    _reset_probe()
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=3,
            total_wall_time=1,
        )
    )

    assert _ConcurrencyProbeRunner.calls == []
    assert all(result["status"] == "skipped" for result in results)


class _RetryFlipClient(FakeRunnerClient):
    """WEB-001 stays incomplete until it succeeds once; others complete fast."""

    solved: ClassVar[set[str]] = set()
    discover_calls: ClassVar[int] = 0

    async def list_challenges(self):
        type(self).discover_calls += 1
        return [
            SimpleNamespace(
                unique_code=code,
                description=f"challenge {code}",
                category="web",
                flag_count=1,
                correct_flag_count=1 if code in type(self).solved else 0,
                difficulty="easy",
                level="L1",
                total_score=100,
                is_completed=code in type(self).solved,
            )
            for code in ("WEB-001", "WEB-002")
        ]


class _RetryProbeRunner(_ConcurrencyProbeRunner):
    fail_cases: ClassVar[set[str]] = set()
    always_fail: ClassVar[bool] = False

    async def run_case(self, *, case, budgets, **kwargs):
        type(self).calls.append(case.id)
        type(self).in_flight += 1
        type(self).max_in_flight = max(type(self).max_in_flight, type(self).in_flight)
        try:
            await asyncio.sleep(0.01)
            first_attempt = type(self).calls.count(case.id) == 1
            if type(self).always_fail or (
                case.id in type(self).fail_cases and first_attempt
            ):
                raise RuntimeError("first attempt fails")
            _RetryFlipClient.solved.add(case.id)
            return {
                "run_id": f"run-{case.id}",
                "benchmark": {"case_id": case.id, "session": {}},
                "status": "finished",
                "success": True,
                "score": 100.0,
                "evaluation": {},
            }
        finally:
            type(self).in_flight -= 1


def test_run_tsec_challenges_retries_incomplete_cases_next_pass(
    monkeypatch, tmp_path: Path
) -> None:
    _reset_probe()
    _RetryFlipClient.solved = set()
    _RetryFlipClient.discover_calls = 0
    _RetryProbeRunner.fail_cases = {"WEB-001"}
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _RetryFlipClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _RetryProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            max_passes=2,
                preflight=False,
        )
    )

    # WEB-001 failed on pass 1, was retried on pass 2 and then solved.
    assert _RetryProbeRunner.calls == ["WEB-001", "WEB-002", "WEB-001"]
    assert _RetryFlipClient.discover_calls == 2
    assert len(results) == 3
    by_challenge = {}
    for result in results:
        by_challenge.setdefault(result["benchmark"]["challenge"], []).append(result["status"])
    assert by_challenge["WEB-001"] == ["error", "finished"]
    assert by_challenge["WEB-002"] == ["finished"]


def test_run_tsec_challenges_stops_when_a_pass_scores_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    _reset_probe()
    _RetryFlipClient.solved = set()
    _RetryFlipClient.discover_calls = 0
    _RetryProbeRunner.fail_cases = {"WEB-001", "WEB-002"}
    _RetryProbeRunner.always_fail = True
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _RetryFlipClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _RetryProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            max_passes=5,
                preflight=False,
        )
    )

    # A scoreless pass no longer resigns: the endgame keeps retrying until the
    # pass budget is spent (article lesson: never stop while attempts remain).
    assert _RetryFlipClient.discover_calls == 5
    assert len(results) == 10


class _FlakyCloseClient(FakeRunnerClient):
    """close_challenge fails N times before succeeding."""

    failures: ClassVar[int] = 0
    calls: ClassVar[int] = 0
    raise_invalid_state: ClassVar[bool] = False

    async def close_challenge(self, code):
        type(self).calls += 1
        if type(self).raise_invalid_state:
            raise InvalidState()
        if type(self).calls <= type(self).failures:
            raise TSecConnError()
        return SimpleNamespace(closed=True)


class InvalidState(Exception):
    pass


class TSecConnError(Exception):
    pass


def _teardown_adapter(client) -> tuple[TSecBenchmarkAdapter, BenchmarkSession]:
    from harness.models import ObjectiveSpec

    adapter = TSecBenchmarkAdapter(client)
    session = BenchmarkSession(
        id="sess-1",
        benchmark="tsec",
        case_id="WEB-001",
        objective=ObjectiveSpec(description="d"),
        targets=[],
        metadata={},
    )
    adapter._sessions[session.id] = AdapterSessionState(
        case=BenchmarkCase(
            id="WEB-001",
            benchmark="tsec",
            domain="web",
            difficulty="easy",
            metadata={"is_completed": False},
        ),
        closed=False,
    )
    return adapter, session


def test_teardown_retries_connection_errors_and_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", _fake_sleep)
    _FlakyCloseClient.failures = 2
    _FlakyCloseClient.calls = 0
    _FlakyCloseClient.raise_invalid_state = False
    adapter, session = _teardown_adapter(_FlakyCloseClient())

    asyncio.run(adapter.teardown(session))

    assert _FlakyCloseClient.calls == 3
    assert adapter._sessions[session.id].closed is True


def test_teardown_never_raises_after_exhausted_retries(monkeypatch, capsys) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", _no_sleep)
    _FlakyCloseClient.failures = 99
    _FlakyCloseClient.calls = 0
    adapter, session = _teardown_adapter(_FlakyCloseClient())

    asyncio.run(adapter.teardown(session))  # must not raise

    assert _FlakyCloseClient.calls == 3
    assert adapter._sessions[session.id].closed is False
    assert "failed after retries" in capsys.readouterr().err


def test_teardown_treats_invalid_state_as_closed(monkeypatch) -> None:
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", _no_sleep)
    _FlakyCloseClient.failures = 0
    _FlakyCloseClient.calls = 0
    _FlakyCloseClient.raise_invalid_state = True
    adapter, session = _teardown_adapter(_FlakyCloseClient())

    asyncio.run(adapter.teardown(session))

    assert adapter._sessions[session.id].closed is True


def test_run_tsec_challenges_applies_the_operator_wall_time_cap(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=7200),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            max_wall_time_seconds=1800,
        )
    )

    assert FakeBenchmarkRunner.last_budgets is not None
    assert FakeBenchmarkRunner.last_budgets.wall_time == 1800


def test_run_tsec_challenges_keeps_a_shorter_declared_wall_clock(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=600),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            max_wall_time_seconds=1800,
        )
    )

    assert FakeBenchmarkRunner.last_budgets is not None
    assert FakeBenchmarkRunner.last_budgets.wall_time == 600


def _challenge(code: str) -> SimpleNamespace:
    return SimpleNamespace(
        unique_code=code,
        description="Authorized web challenge",
        category="web",
        flag_count=1,
        correct_flag_count=0,
        difficulty="easy",
        level="L2",
        total_score=100,
        is_completed=False,
    )


class FakeMultiChallengeClient(FakeRunnerClient):
    async def list_challenges(self):
        return [_challenge("WEB-001"), _challenge("WEB-002"), _challenge("WEB-003")]

    async def start_challenge(self, code):
        return SimpleNamespace(container_addr=[f"10.0.0.{code[-1]}:8080"])

    async def close_challenge(self, code):
        self.closed = True
        return SimpleNamespace(closed=True)


# The SDK raises these two by class name; the harness classifies without
# importing it, so the doubles only have to carry the same names and messages.
VpnCheckError = type("VpnCheckError", (RuntimeError,), {})


class NoVpnClient(FakeRunnerClient):
    async def check_vpn(self):
        raise VpnCheckError("VPN检测未通过,请检查靶场VPN网络配置")


class InstanceConflictClient(FakeMultiChallengeClient):
    """The platform refuses a new start while three instances are still open."""

    async def start_challenge(self, code):
        raise InvalidState("max active challenge instances reached (3)")


class RecordingRunner(FakeBenchmarkRunner):
    kwargs_by_case: ClassVar[dict[str, dict]] = {}

    async def run_case(self, *, case, budgets, **kwargs):
        type(self).kwargs_by_case[case.id] = kwargs
        return await super().run_case(case=case, budgets=budgets, **kwargs)


class ProvisioningRunner(FakeBenchmarkRunner):
    """Exercises the adapter's provision path the way the real runner does."""

    async def run_case(self, *, case, adapter, budgets, **kwargs):
        await adapter.provision(case)
        return await super().run_case(case=case, budgets=budgets, **kwargs)


def test_sweep_stops_before_any_run_directory_when_the_platform_is_down(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", NoVpnClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)
    summary = tmp_path / "sweep.json"

    with pytest.raises(TSecPlatformError) as raised:
        asyncio.run(
            run_tsec_challenges(
                config=TSecConfig(base_url="https://example.invalid", token="secret"),
                agent=_runner_agent(),
                budgets=BudgetSpec(wall_time=30),
                runs_root=tmp_path / "runs",
                run_all=True,
                summary_out=summary,
            )
        )

    assert raised.value.kind == "vpn_unreachable"
    runs_root = tmp_path / "runs"
    assert not runs_root.exists() or list(runs_root.iterdir()) == []
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "harness/tsec-sweep/v1"
    assert payload["platform_error"]["kind"] == "vpn_unreachable"
    assert payload["cases"] == []
    assert payload["tallies"] == {"selected": 0, "completed": 0, "skipped": 0, "failed": 0}


def test_sweep_records_a_case_local_failure_and_keeps_going(
    monkeypatch, tmp_path: Path
) -> None:
    class OneBadCase(FakeMultiChallengeClient):
        async def start_challenge(self, code):
            if code == "WEB-002":
                raise RuntimeError("challenge container refused to start")
            return await super().start_challenge(code)

    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", OneBadCase)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", ProvisioningRunner)
    summary = tmp_path / "sweep.json"

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            run_all=True,
            parallel=2,
            summary_out=summary,
        )
    )

    assert [item["benchmark"]["case_id"] for item in results] == [
        "WEB-001",
        "WEB-002",
        "WEB-003",
    ]
    assert results[1]["status"] == "error"
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["failures"] == [
        {"case_id": "WEB-002", "failure_kind": "case_error", "error_type": "RuntimeError"}
    ]
    assert payload["tallies"]["completed"] == 2
    assert payload["tallies"]["failed"] == 1


async def _fast_sleep(_seconds: float) -> None:
    return None


def test_instance_conflicts_are_waited_out_and_the_sweep_continues(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", InstanceConflictClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", ProvisioningRunner)
    summary = tmp_path / "sweep.json"

    # No TSecPlatformError: the conflict is a scheduling race, not an outage.
    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            run_all=True,
            parallel=1,
            summary_out=summary,
        )
    )

    assert len(results) == 3
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["platform_error"] is None
    kinds = {item["case_id"]: item["failure_kind"] for item in payload["failures"]}
    # Every case exhausted its conflict waits and is recorded per-case; the
    # sweep itself was never aborted (no dispatch_aborted entries anywhere).
    assert kinds == {
        "WEB-001": "instance_conflict",
        "WEB-002": "instance_conflict",
        "WEB-003": "instance_conflict",
    }
    assert all(result["status"] == "error" for result in results)


def test_start_waits_out_a_transient_instance_conflict(monkeypatch, tmp_path: Path) -> None:
    waited: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", record_sleep)

    class FlakyConflictClient(FakeRunnerClient):
        conflicts_left = 2

        async def start_challenge(self, code):
            if type(self).conflicts_left > 0:
                type(self).conflicts_left -= 1
                raise InvalidState("max active challenge instances reached (3)")
            return SimpleNamespace(container_addr=["10.0.0.2:80"])

    adapter = TSecBenchmarkAdapter(FlakyConflictClient())
    case = BenchmarkCase(
        id="WEB-001",
        benchmark="tsec",
        domain="web",
        difficulty="easy",
        metadata={"is_completed": False},
    )

    started = asyncio.run(adapter._start(case.id))

    assert started.container_addr == ["10.0.0.2:80"]
    assert waited == [tsec_adapter.INSTANCE_CONFLICT_WAIT_SECONDS] * 2


def test_sweep_resume_skips_locally_solved_cases_and_seeds_the_rest(
    monkeypatch, tmp_path: Path
) -> None:
    solved_run = tmp_path / "tsec_WEB-001_deadbeef"
    solved_run.mkdir()
    (solved_run / "result.json").write_text(
        json.dumps({"status": "objective_completed", "success": True}), encoding="utf-8"
    )
    failed_run = tmp_path / "tsec_WEB-002_cafebabe"
    (failed_run / "agent-workspace").mkdir(parents=True)
    (failed_run / "agent-workspace" / "agent.summary.md").write_text(
        "## Confirmed facts\nprior attempt\n", encoding="utf-8"
    )
    (failed_run / "result.json").write_text(
        json.dumps({"status": "finished", "success": False}), encoding="utf-8"
    )
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeMultiChallengeClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", RecordingRunner)
    RecordingRunner.kwargs_by_case = {}
    summary = tmp_path / "sweep.json"

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            run_all=True,
            summary_out=summary,
        )
    )

    # Fair ordering may reorder equal/solved-history cases; the set is what matters.
    assert {item["benchmark"]["case_id"] for item in results} == {"WEB-002", "WEB-003"}
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["skipped"] == [
        {"case_id": "WEB-001", "run_id": "tsec_WEB-001_deadbeef", "reason": "local-success"}
    ]
    assert (
        RecordingRunner.kwargs_by_case["WEB-002"]["seed_notes_from"]
        == tmp_path / "tsec_WEB-002_cafebabe"
    )
    assert RecordingRunner.kwargs_by_case["WEB-003"]["seed_notes_from"] is None


def test_sweep_reruns_an_explicitly_named_challenge(monkeypatch, tmp_path: Path) -> None:
    solved_run = tmp_path / "tsec_WEB-001_deadbeef"
    solved_run.mkdir()
    (solved_run / "result.json").write_text(
        json.dumps({"status": "objective_completed", "success": True}), encoding="utf-8"
    )
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeMultiChallengeClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            summary_out=tmp_path / "sweep.json",
        )
    )

    assert [item["benchmark"]["case_id"] for item in results] == ["WEB-001"]


def test_platform_failure_classification_covers_the_three_operator_states() -> None:
    assert classify_platform_failure(VpnCheckError("VPN检测未通过")) == "vpn_unreachable"
    assert (
        classify_platform_failure(InvalidState("max active challenge instances reached (3)"))
        == "instance_conflict"
    )
    connection = type("TSecConnectionError", (RuntimeError,), {})
    transport = connection("request failed: [Errno -3] Temporary failure in name resolution")
    assert classify_platform_failure(transport) == "platform_unreachable"
    assert classify_platform_failure(RuntimeError("challenge not found")) is None


def _write_cli_agent(path: Path) -> Path:
    path.write_text(
        "apiVersion: harness/v1\n"
        "id: pi-tsec\n"
        "type: pi\n"
        "image: pi:test\n"
        "network: host\n"
        "pi:\n"
        "  provider: fake\n"
        "  model: m\n",
        encoding="utf-8",
    )
    return path


def test_tsec_cli_separates_platform_outage_from_partial_failure(
    monkeypatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from harness import cli

    agent_path = _write_cli_agent(tmp_path / "agent.yaml")
    runs_root = tmp_path / "runs"
    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("BENCHMARK_TOKEN", "secret")
    runner = CliRunner()
    args = [
        "tsec",
        "--agent",
        str(agent_path),
        "--runs-root",
        str(runs_root),
        "--all",
    ]

    async def platform_down(**_kwargs):
        raise TSecPlatformError(kind="vpn_unreachable", detail="health check timed out")

    monkeypatch.setattr(cli, "run_tsec_challenges", platform_down)
    outage = runner.invoke(cli.app, args)
    assert outage.exit_code == 3
    assert "do not retry this sweep in a loop" in outage.stderr

    async def partial_failure(*, summary_out, **_kwargs):
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(
            json.dumps({"failures": [{"case_id": "WEB-001"}]}), encoding="utf-8"
        )
        return []

    monkeypatch.setattr(cli, "run_tsec_challenges", partial_failure)
    incomplete = runner.invoke(cli.app, args)
    assert incomplete.exit_code == 4
    assert "did not produce a run" in incomplete.stderr

    async def clean(*, summary_out, **_kwargs):
        summary_out.parent.mkdir(parents=True, exist_ok=True)
        summary_out.write_text(json.dumps({"failures": []}), encoding="utf-8")
        return []

    monkeypatch.setattr(cli, "run_tsec_challenges", clean)
    assert runner.invoke(cli.app, args).exit_code == 0


class ConcurrentRunner(FakeBenchmarkRunner):
    active = 0
    peak = 0

    async def run_case(self, *, case, budgets, **kwargs):
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        try:
            await asyncio.sleep(0.01)
            return await super().run_case(case=case, budgets=budgets, **kwargs)
        finally:
            type(self).active -= 1


def test_run_tsec_challenges_bounds_concurrency_to_the_parallel_limit(
    monkeypatch, tmp_path: Path
) -> None:
    _reset_probe()
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)
    _ConcurrencyProbeRunner.fail_cases = set()

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            run_all=True,
            parallel=2,
        )
    )

    assert len(results) == 3
    assert _ConcurrencyProbeRunner.max_in_flight == 2


def test_run_tsec_challenges_rejects_parallel_above_the_platform_cap(tmp_path: Path) -> None:
    with pytest.raises(TSecAdapterError):
        asyncio.run(
            run_tsec_challenges(
                config=TSecConfig(base_url="https://example.invalid", token="secret"),
                agent=_runner_agent(),
                budgets=BudgetSpec(wall_time=30),
                runs_root=tmp_path,
                run_all=True,
                parallel=MAX_PARALLEL_CHALLENGES + 1,
            )
        )


async def _fake_sleep(_seconds: float) -> None:
    return None


class FakeSDK:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token
        self.calls: list[tuple] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True

    async def list_challenges(self):
        self.calls.append(("list_challenges",))
        return ["case"]

    async def start_challenge(self, unique_code: str):
        self.calls.append(("start_challenge", unique_code))
        return {"code": unique_code}

    async def submit_flag(self, unique_code: str, flag: str):
        self.calls.append(("submit_flag", unique_code, flag))
        return {"correct": True}

    async def close_challenge(self, unique_code: str):
        self.calls.append(("close_challenge", unique_code))
        return {"closed": True}

    async def get_hint(self, unique_code: str):
        self.calls.append(("get_hint", unique_code))
        return {"hint": "peek"}


def test_tsec_client_adapter_forwards_sdk_config_and_every_call() -> None:
    captured: dict[str, str] = {}
    sdk = FakeSDK("unused", "unused")

    def factory(base_url: str, token: str) -> FakeSDK:
        captured.update(base_url=base_url, token=token)
        return sdk

    adapter = TSecClientAdapter(
        TSecConfig(base_url="https://tsec.example", token="tok-123"),
        sdk_factory=factory,
    )

    async def scenario() -> None:
        async with adapter as client:
            await client.list_challenges()
            await client.start_challenge("WEB-001")
            await client.submit_flag("WEB-001", "flag{x}")
            await client.get_hint("WEB-001")
            await client.close_challenge("WEB-001")

    asyncio.run(scenario())

    assert captured == {"base_url": "https://tsec.example", "token": "tok-123"}
    assert sdk.calls == [
        ("list_challenges",),
        ("start_challenge", "WEB-001"),
        ("submit_flag", "WEB-001", "flag{x}"),
        ("get_hint", "WEB-001"),
        ("close_challenge", "WEB-001"),
    ]
    assert sdk.entered and sdk.exited


def test_preflight_uses_configurable_tsec_env_names(monkeypatch) -> None:
    monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
    monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
    monkeypatch.setenv("ALT_BASE_URL", "https://tsec.example")
    monkeypatch.setenv("ALT_TOKEN", "tok")
    root = Path(__file__).resolve().parents[1]
    task = root / "benchmarks" / "examples" / "hello" / "task.yaml"
    agent = root / "agents" / "examples" / "pi.yaml"

    report = run_preflight(
        load_task(task),
        load_agent(agent),
        task_path=task,
        require_tsec=True,
        tsec_base_url_env="ALT_BASE_URL",
        tsec_token_env="ALT_TOKEN",
    )

    base_url = report.by_name("tsec_base_url")
    credential = report.by_name("tsec_credential")
    assert base_url.ok and "ALT_BASE_URL" in base_url.detail
    assert credential.ok and "ALT_TOKEN" in credential.detail


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


def _clock_sleep(clock: FakeClock, recorded: list[float]):
    async def sleep(seconds: float) -> None:
        recorded.append(seconds)
        clock.now += seconds

    return sleep


class RateLimitError(Exception):
    status_code = 429


class FlakyListSDK(FakeSDK):
    def __init__(self, base_url: str, token: str) -> None:
        super().__init__(base_url, token)
        self.list_failures = 0
        self.list_attempts = 0

    async def list_challenges(self):
        self.list_attempts += 1
        if self.list_failures > 0:
            self.list_failures -= 1
            raise RateLimitError()
        return await super().list_challenges()


class FailingStartSDK(FakeSDK):
    async def start_challenge(self, unique_code: str):
        raise RuntimeError("start rejected")


class HintCountingClient(FakeTSecClient):
    def __init__(self) -> None:
        super().__init__()
        self.hint_calls = 0

    async def get_hint(self, unique_code: str):
        self.hint_calls += 1
        return await super().get_hint(unique_code)


class FailingHintClient(FakeTSecClient):
    def __init__(self) -> None:
        super().__init__()
        self.hint_calls = 0

    async def get_hint(self, unique_code: str):
        self.hint_calls += 1
        raise RuntimeError("hint service down")


def _limited_adapter(
    sdk: FakeSDK,
) -> tuple[TSecClientAdapter, FakeClock, list[float]]:
    clock = FakeClock()
    slept: list[float] = []
    adapter = TSecClientAdapter(
        TSecConfig(base_url="https://tsec.example", token="tok"),
        sdk_factory=lambda _url, _token: sdk,
        limiter=_TokenBucket(clock=clock.time, sleep=_clock_sleep(clock, slept)),
        sleep=_clock_sleep(clock, slept),
    )
    return adapter, clock, slept


def test_tsec_client_rate_limits_calls_to_three_per_second() -> None:
    sdk = FakeSDK("unused", "unused")
    adapter, _clock, slept = _limited_adapter(sdk)

    async def scenario() -> None:
        for _ in range(4):
            await adapter.list_challenges()

    asyncio.run(scenario())

    assert len(sdk.calls) == 4
    assert slept == [pytest.approx(1 / 3)]


def test_tsec_client_caps_instances_and_releases_on_close() -> None:
    sdk = FakeSDK("unused", "unused")
    adapter, _clock, _slept = _limited_adapter(sdk)

    async def scenario() -> None:
        for code in ("A", "B", "C"):
            await adapter.start_challenge(code)
        assert adapter.open_instances == {"A", "B", "C"}

        with pytest.raises(TSecAdapterError):
            await adapter.start_challenge("D")
        with pytest.raises(TSecAdapterError):
            await adapter.start_challenge("B")

        await adapter.close_challenge("A")
        await adapter.close_challenge("A")  # idempotent release
        await adapter.start_challenge("D")
        assert adapter.open_instances == {"B", "C", "D"}

    asyncio.run(scenario())


def test_tsec_client_releases_instance_when_start_fails() -> None:
    sdk = FailingStartSDK("unused", "unused")
    adapter, _clock, _slept = _limited_adapter(sdk)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await adapter.start_challenge("X")
        assert adapter.open_instances == frozenset()

    asyncio.run(scenario())


def test_tsec_client_retries_429_once_then_fails_closed() -> None:
    sdk = FlakyListSDK("unused", "unused")
    adapter, _clock, slept = _limited_adapter(sdk)

    async def scenario() -> None:
        sdk.list_failures = 1
        assert await adapter.list_challenges() == ["case"]

        sdk.list_failures = 2
        with pytest.raises(RateLimitError):
            await adapter.list_challenges()

    asyncio.run(scenario())

    assert sdk.list_attempts == 4
    assert slept == [
        TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS,
        TSEC_RATE_LIMIT_RETRY_DELAY_SECONDS,
    ]


def test_tsec_adapter_requests_hint_once_per_case() -> None:
    async def scenario() -> None:
        client = HintCountingClient()
        adapter = TSecBenchmarkAdapter(client, use_hint=False)
        cases = await adapter.discover()
        session = await adapter.provision(cases[0])

        assert session.metadata["hint_trigger"] is None
        first = await adapter.request_hint("WEB-001")
        second = await adapter.request_hint("WEB-001")

        assert first == "look at the service"
        assert second == first
        assert client.hint_calls == 1
        assert session.metadata["hint_used"] is True
        assert session.metadata["hint_trigger"] == "threshold"
        assert session.metadata["hint"] == "look at the service"

    asyncio.run(scenario())


def test_tsec_adapter_hint_failure_is_recorded_and_not_retried() -> None:
    async def scenario() -> None:
        client = FailingHintClient()
        adapter = TSecBenchmarkAdapter(client, use_hint=False)
        cases = await adapter.discover()
        session = await adapter.provision(cases[0])

        assert await adapter.request_hint("WEB-001") is None
        assert await adapter.request_hint("WEB-001") is None
        assert client.hint_calls == 1
        assert session.metadata["hint_used"] is False
        assert session.metadata["hint_error"] is True
        assert session.metadata["hint_trigger"] == "threshold"

    asyncio.run(scenario())


def test_tsec_adapter_immediate_hint_is_not_requested_again_by_the_threshold() -> None:
    """--hint plus --hint-threshold must pay for one hint view, not two."""

    async def scenario() -> None:
        client = HintCountingClient()
        adapter = TSecBenchmarkAdapter(client, use_hint=True)
        cases = await adapter.discover()
        session = await adapter.provision(cases[0])

        assert client.hint_calls == 1
        assert session.metadata["hint_trigger"] == "immediate"

        assert await adapter.request_hint("WEB-001") == "look at the service"
        assert client.hint_calls == 1
        assert session.metadata["hint_trigger"] == "immediate"
        assert session.metadata["hint"] == "look at the service"
        # The cached reply is not a fresh fetch, so no failure flag is written.
        assert "hint_error" not in session.metadata

    asyncio.run(scenario())


class MetricsRunner(FakeBenchmarkRunner):
    """A runner whose result carries the run metric block the sweep aggregates."""

    async def run_case(self, *, case, budgets, **kwargs):
        result = await super().run_case(case=case, budgets=budgets, **kwargs)
        result["metrics"] = {
            "duration_ms": 600000,
            "model_calls": 16,
            "tool_calls": 20,
            "submissions_attempted": 1,
            "first_submission_ms": 594000,
            "submitted_at_fraction": 0.99,
            "steer_counts": {
                "world": 3,
                "verification": 0,
                "repetition": 1,
                "budget": 1,
                "finalize": 0,
            },
            "tool_time_ms": 123456,
            "context_tokens_final": 20583,
        }
        return result


def test_sweep_summary_carries_the_run_metrics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", MetricsRunner)
    summary = tmp_path / "sweep.json"

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=600),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            seed=1,
            summary_out=summary,
        )
    )

    payload = json.loads(summary.read_text(encoding="utf-8"))
    case = payload["cases"][0]
    assert case["first_submission_ms"] == 594000
    assert case["submitted_at_fraction"] == 0.99
    assert case["steer_counts"]["world"] == 3
    assert case["tool_time_ms"] == 123456
    assert case["context_tokens_final"] == 20583


def test_sweep_summary_defaults_missing_metrics_to_zero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeRunnerClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", FakeBenchmarkRunner)
    summary = tmp_path / "sweep.json"

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=600),
            runs_root=tmp_path,
            challenge_code="WEB-001",
            seed=1,
            summary_out=summary,
        )
    )

    case = json.loads(summary.read_text(encoding="utf-8"))["cases"][0]
    assert case["first_submission_ms"] is None
    assert case["submitted_at_fraction"] is None
    assert case["steer_counts"] == {}
    assert case["tool_time_ms"] == 0
    assert case["context_tokens_final"] == 0


def test_pass_boundary_discover_retries_transient_network_failures(
    monkeypatch, tmp_path: Path
) -> None:
    _reset_probe()
    calls: list[str] = []

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", fast_sleep)

    class FlakyDiscoverClient(FakeMultiChallengeClient):
        failures_left = 2

        async def list_challenges(self):
            calls.append("discover")
            if type(self).failures_left > 0:
                type(self).failures_left -= 1
                raise TSecConnError("request failed: ConnectTimeout")
            return await super().list_challenges()

    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FlakyDiscoverClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=3,
            max_passes=1,
        )
    )

    # The preflight and listing survived the two transient failures and the
    # single pass completed every case.
    assert len(results) == 3
    assert all(result["status"] == "finished" for result in results)
    assert calls.count("discover") >= 3


def test_hosted_config_skips_the_vpn_probe(monkeypatch) -> None:
    from harness.benchmark.tsec_client import load_tsec_config

    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://benchmark.invalid")
    monkeypatch.setenv("BENCHMARK_TOKEN", "secret")
    monkeypatch.setenv("TSEC_SKIP_VPN_CHECK", "1")

    config = load_tsec_config()
    assert config.check_vpn is False

    class ExplodingVpnClient(FakeRunnerClient):
        async def check_vpn(self):
            raise AssertionError("check_vpn must not be called when disabled")

        async def list_challenges(self):
            return []

    # The preflight proves connectivity by listing instead of probing.
    asyncio.run(
        tsec_adapter._platform_preflight(ExplodingVpnClient(), check_vpn=False)
    )


def test_default_config_keeps_the_vpn_probe(monkeypatch) -> None:
    monkeypatch.setenv("BENCHMARK_BASE_URL", "https://benchmark.invalid")
    monkeypatch.setenv("BENCHMARK_TOKEN", "secret")
    monkeypatch.delenv("TSEC_SKIP_VPN_CHECK", raising=False)

    from harness.benchmark.tsec_client import load_tsec_config

    assert load_tsec_config().check_vpn is True


def test_persistent_conflicts_cool_dispatch_instead_of_burning_the_queue(
    monkeypatch, tmp_path: Path
) -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", InstanceConflictClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", ProvisioningRunner)

    results = asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=30),
            runs_root=tmp_path,
            run_all=True,
            concurrency=1,
            max_passes=1,
        )
    )

    # Every case still records its own failure, but after two exhausted
    # conflicts the dispatcher cools down instead of hammering the platform
    # with the rest of the queue.
    assert len(results) == 3
    cooldowns = [s for s in sleeps if s > tsec_adapter.CONFLICT_COOLDOWN_SECONDS - 10]
    assert len(cooldowns) >= 1


def test_pass_boundary_reaps_leaked_challenge_instances(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(tsec_adapter.asyncio, "sleep", no_sleep)
    _FlakyCloseClient.failures = 99  # every close fails in pass 1
    _FlakyCloseClient.calls = 0
    _FlakyCloseClient.raise_invalid_state = False
    adapter, session = _teardown_adapter(_FlakyCloseClient())

    asyncio.run(adapter.teardown(session))  # gives up; instance leaks
    assert adapter._sessions[session.id].closed is False

    _FlakyCloseClient.failures = 0  # platform turbulence over
    _FlakyCloseClient.calls = 0

    reaped = asyncio.run(adapter.reap_unclosed_sessions())

    assert reaped == 1
    assert adapter._sessions[session.id].closed is True


def test_case_budget_scales_with_difficulty() -> None:
    base = BudgetSpec(wall_time=1800)

    def case(difficulty: str) -> BenchmarkCase:
        return BenchmarkCase(
            id="WEB-001",
            benchmark="tsec",
            domain="web",
            difficulty=difficulty,
            metadata={"is_completed": False},
        )

    assert tsec_adapter._case_budget(case("hard"), base).wall_time == 3600
    # Non-hard difficulties keep the declared budget untouched.
    assert tsec_adapter._case_budget(case("medium"), base).wall_time == 1800
    assert tsec_adapter._case_budget(case("easy"), base).wall_time == 1800
    assert tsec_adapter._case_budget(case("unknown-level"), base).wall_time == 1800
    # An explicit operator ceiling still wins over the difficulty map.
    assert (
        tsec_adapter._case_budget(case("hard"), base, ceiling=1800).wall_time == 1800
    )


def test_least_attempted_cases_are_dispatched_first(monkeypatch, tmp_path: Path) -> None:
    _reset_probe()
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", _MultiCaseClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", _ConcurrencyProbeRunner)

    # WEB-001 already has two attempts in this runs root; the others have none.
    for attempt in range(2):
        previous = tmp_path / f"tsec_WEB-001_{attempt:08x}"
        (previous / "agent-workspace" / "notes").mkdir(parents=True)
        (previous / "agent-workspace" / "notes" / "n.md").write_text("x", encoding="utf-8")

    asyncio.run(
        run_tsec_challenges(
            config=TSecConfig(base_url="https://example.invalid", token="secret"),
            agent=_runner_agent(),
            budgets=BudgetSpec(wall_time=60),
            runs_root=tmp_path,
            run_all=True,
            concurrency=3,
            max_passes=1,
            resume=False,
        )
    )

    # Zero-attempt cases were dispatched before the twice-attempted one.
    assert _ConcurrencyProbeRunner.calls[-1] == "WEB-001"
    assert set(_ConcurrencyProbeRunner.calls[:2]) == {"WEB-002", "WEB-003"}


def test_loot_gate_blocks_repeat_submissions_across_sessions() -> None:
    calls: list[str] = []

    class RecordingClient(_MultiCaseClient):
        async def submit_flag(self, code, flag):
            calls.append((code, flag))
            return SimpleNamespace(
                correct=True, awarded=100, cumulative_score=100,
                correct_flag_count=1, total_flag_count=2, matched_flag_index=0,
            )

    client = RecordingClient()
    adapter = TSecBenchmarkAdapter(client)
    session = BenchmarkSession(
        id="sess-1",
        benchmark="tsec",
        case_id="WEB-001",
        objective={"description": "d"},
        targets=[],
        metadata={},
    )

    adapter._sessions[session.id] = AdapterSessionState(
        case=BenchmarkCase(
            id="WEB-001", benchmark="tsec", domain="web", difficulty="easy",
            metadata={"is_completed": False},
        ),
        closed=False,
    )
    first = asyncio.run(adapter.submit(session, Submission(type="flag", value="flag{one}")))
    assert first.accepted is True
    # Second session (continuation) proposes the same value: the gate blocks it.
    second = asyncio.run(adapter.submit(session, Submission(type="flag", value="flag{one}")))
    assert second.accepted is False
    assert second.metadata.get("loot_gate") == "already_submitted"
    assert len(calls) == 1
