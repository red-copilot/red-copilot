from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from harness.benchmark import tsec_adapter
from harness.benchmark.base import BenchmarkCase, Submission
from harness.benchmark.tsec_adapter import (
    TSecBenchmarkAdapter,
    extract_flags,
    run_tsec_challenges,
)
from harness.benchmark.tsec_client import TSecConfig
from harness.models import AgentSpec, BudgetSpec
from harness.runtime.solver_profile import SolverProfile


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
    last_skills_root: Path | None = None

    def __init__(self, *, runs_root: Path, skills_root: Path = Path("skills")):
        self.runs_root = runs_root
        type(self).last_skills_root = Path(skills_root)

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
                "solver_profile": (
                    kwargs.get("solver_profile", SolverProfile.PI_WORLD_HEURISTIC).value
                ),
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
            skills_root=tmp_path / "custom-skills",
            challenge_code="WEB-001",
            seed=1,
            solver_profile=SolverProfile.PI_WORLD,
            max_unconfirmed_progress=200,
            max_replans=20,
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
    assert result["benchmark"]["solver_profile"] == "pi-world"
    assert FakeBenchmarkRunner.last_skills_root == tmp_path / "custom-skills"
    assert FakeBenchmarkRunner.last_kwargs is not None
    assert FakeBenchmarkRunner.last_kwargs["solver_profile"] is SolverProfile.PI_WORLD
    assert FakeBenchmarkRunner.last_kwargs["max_unconfirmed_progress"] == 200
    assert FakeBenchmarkRunner.last_kwargs["max_replans"] == 20


def test_run_tsec_challenges_applies_platform_wall_time_ceiling(
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
    assert FakeBenchmarkRunner.last_budgets.wall_time == 3600
