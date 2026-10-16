from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.benchmark import tsec_adapter
from harness.benchmark.base import BenchmarkCase, Submission
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
    _TokenBucket,
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
    concurrent_peak: int = 0

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


def test_run_tsec_challenges_honours_the_declared_wall_clock(
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
        )
    )

    assert FakeBenchmarkRunner.last_budgets is not None
    assert FakeBenchmarkRunner.last_budgets.wall_time == 7200


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
    monkeypatch.setattr(tsec_adapter, "TSecClientAdapter", FakeMultiChallengeClient)
    monkeypatch.setattr(tsec_adapter, "BenchmarkRunner", ConcurrentRunner)
    ConcurrentRunner.peak = 0

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
    assert ConcurrentRunner.peak == 2


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


def _fake_sleep(clock: FakeClock, recorded: list[float]):
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
        limiter=_TokenBucket(clock=clock.time, sleep=_fake_sleep(clock, slept)),
        sleep=_fake_sleep(clock, slept),
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
