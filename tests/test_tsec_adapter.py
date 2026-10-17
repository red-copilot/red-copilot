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
from harness.benchmark.tsec_client import TSecAdapterError, TSecConfig
from harness.models import AgentSpec, BudgetSpec


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
        )
    )

    # Two consecutive zero-score passes stop the loop; the first one alone
    # would have been retried (transient-burst tolerance).
    assert _RetryFlipClient.discover_calls == 2
    assert len(results) == 4


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


def test_run_tsec_challenges_parallel_alias_bounds_concurrency(
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
