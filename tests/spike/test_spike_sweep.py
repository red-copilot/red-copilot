"""Sweep orchestration: layouts, exit codes and the round loop with fakes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Self

import pytest

from spike_swarm import sweep as sweep_module
from spike_swarm.cli import build_parser, config_from_args
from spike_swarm.containers import RpcCounters
from spike_swarm.models import Challenge, LayoutError, SpikeError
from spike_swarm.platform import FlagVerdict, PlatformUnusable, StartedChallenge
from spike_swarm.sweep import (
    EXIT_NO_RUN,
    EXIT_OK,
    EXIT_PLATFORM,
    RunLayout,
    SessionState,
    SweepConfig,
    TaskOutcome,
    exit_code_for,
    plan_only,
    redact_command,
    run_task,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path, **overrides: object) -> SweepConfig:
    values: dict[str, object] = {
        "runs_root": tmp_path / "runs",
        "agent_dir": REPO_ROOT / "spike" / "agent",
        "knowledge_roots": (REPO_ROOT / "knowledge",),
        "image": "harness/pi-kali:local",
        "provider": "openai",
        "model": "test-model",
        "wall_time": 60,
        "round_seconds": 5,
    }
    values.update(overrides)
    return SweepConfig(**values)  # type: ignore[arg-type]


class FakePi:
    """Stands in for one Pi RPC session, writing scripted contribution lines."""

    def __init__(self, paths, script: list[tuple[list[str], list[dict[str, object]]]]) -> None:
        self.paths = paths
        self.script = list(script)
        self.alive = True
        self.settled = True
        self.counters = RpcCounters()
        self.provider = "openai"
        self.model = "test-model"
        self.thinking = "high"
        self.session_id = f"fake-{paths.session_id}"
        self.log_path = None
        self.raw_log_path = None
        self.briefs: list[str] = []

    def _act(self) -> None:
        if not self.script:
            return
        inbox, goals = self.script.pop(0)
        with self.paths.inbox_file.open("a", encoding="utf-8") as handle:
            for line in inbox:
                handle.write(line + "\n")
        with self.paths.goals_file.open("a", encoding="utf-8") as handle:
            for op in goals:
                handle.write(json.dumps(op) + "\n")

    async def prompt(self, message: str) -> None:
        self.briefs.append(message)
        self._act()
        self.settled = False

    async def send_round(self, message: str) -> None:
        self.briefs.append(message)
        self._act()

    async def wait_settled(self, *, timeout: float) -> bool:
        await asyncio.sleep(0.002)
        self.settled = True
        return True

    async def stop(self) -> None:
        self.alive = False


class FakeClient:
    def __init__(self, *, verdicts: dict[str, bool] | None = None) -> None:
        self.verdicts = verdicts or {}
        self.submitted: list[str] = []
        self.closed: list[str] = []
        self.started: list[str] = []
        self.fail_start: PlatformUnusable | None = None

    async def start_challenge(self, code: str) -> StartedChallenge:
        if self.fail_start is not None:
            raise self.fail_start
        self.started.append(code)
        return StartedChallenge(code=code, addresses=("10.0.0.1",))

    async def submit_flag(self, code: str, flag: str) -> FlagVerdict:
        self.submitted.append(flag)
        accepted = self.verdicts.get(flag, False)
        return FlagVerdict(accepted=accepted, detail="correct" if accepted else "rejected")

    async def close_challenge(self, code: str) -> None:
        self.closed.append(code)

    def events_for(self, code: str) -> list[dict[str, object]]:
        return []


def _install_fakes(monkeypatch: pytest.MonkeyPatch, scripts: dict[str, list]) -> None:
    async def fake_launch(*, config, layout, paths, challenge, objective, addresses, thinking):
        del config, layout, challenge, objective, addresses, thinking
        paths.inbox_file.parent.mkdir(parents=True, exist_ok=True)
        paths.goals_file.parent.mkdir(parents=True, exist_ok=True)
        return SessionState(
            session_id=paths.session_id,
            index=paths.index,
            paths=paths,
            container=f"fake-{paths.session_id}",
            session=FakePi(paths, scripts[paths.session_id]),
        )

    async def fake_destroy(name: str) -> None:
        del name

    monkeypatch.setattr(sweep_module, "launch_session", fake_launch)
    monkeypatch.setattr(sweep_module, "destroy_container", fake_destroy)


def test_exit_codes_follow_the_platform_and_run_contract() -> None:
    ok = TaskOutcome(
        code="A",
        difficulty="easy",
        status="abandoned",
        exit_reason="window",
        rounds=2,
        started=True,
        sessions=[{"id": "s1", "rounds": 2}],
    )
    empty = TaskOutcome(
        code="B", difficulty="easy", status="failed", exit_reason="start", rounds=0, started=False
    )
    idle = TaskOutcome(
        code="C",
        difficulty="easy",
        status="blocked",
        exit_reason="all_sessions_dead",
        rounds=3,
        started=True,
        sessions=[{"id": "s1", "rounds": 0}],
    )
    assert ok.wrote_run is True
    assert empty.wrote_run is False
    assert idle.wrote_run is False
    assert exit_code_for(platform_kind=None, outcomes=[ok]) == EXIT_OK
    assert exit_code_for(platform_kind=None, outcomes=[ok, empty]) == EXIT_NO_RUN
    assert exit_code_for(platform_kind=None, outcomes=[ok, idle]) == EXIT_NO_RUN
    assert exit_code_for(platform_kind=None, outcomes=[]) == EXIT_NO_RUN
    assert exit_code_for(platform_kind="vpn_unreachable", outcomes=[ok]) == EXIT_PLATFORM


def test_run_layout_creates_the_documented_tree(tmp_path: Path) -> None:
    layout = RunLayout(root=tmp_path / "WEB-1", code="WEB-1")
    layout.prepare()
    assert layout.graph_path.read_text(encoding="utf-8").startswith("# Intent-fact graph")
    assert layout.inbox_dir.is_dir() and layout.goals_dir.is_dir() and layout.snapshots_dir.is_dir()
    session = layout.session(index=2, session_id="s2")
    assert session.directory.name == "02-s2"
    assert session.inbox_file == layout.inbox_dir / "s2.md"
    assert session.workspace.is_dir() and session.notes.is_dir()


def test_run_layout_refuses_a_symlinked_run_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "run"
    link.symlink_to(real, target_is_directory=True)
    layout = RunLayout(root=link, code="X")
    with pytest.raises(LayoutError):
        layout.prepare()


def test_redact_command_masks_credentials() -> None:
    command = ["docker", "create", "-e", "OPENAI_API_KEY=sk-live", "-e", "PI_OFFLINE=1"]
    assert redact_command(command) == [
        "docker",
        "create",
        "-e",
        "OPENAI_API_KEY=<redacted>",
        "-e",
        "PI_OFFLINE=1",
    ]


def test_plan_only_needs_explicit_challenges(tmp_path: Path) -> None:
    with pytest.raises(SpikeError):
        plan_only(_config(tmp_path))
    plan = plan_only(_config(tmp_path, challenges=("WEB-1",)))
    assert len(plan) == 3
    assert all(entry["challenge"] == "WEB-1" for entry in plan)
    assert "--read-only" in plan[0]["create"]

    two = plan_only(_config(tmp_path, challenges=("WEB-1", "WEB/2")))
    assert len(two) == 6
    assert {entry["challenge"] for entry in two} == {"WEB-1", "WEB/2"}
    assert two[0]["container"] == "spike-WEB-1-s1"
    assert two[3]["container"] == "spike-WEB-2-s1"  # a slash cannot reach Docker
    assert str(tmp_path / "runs" / "WEB" / "2" / "sessions") in two[3]["create"]


def test_config_from_args_rejects_limits_above_the_platform_cap(tmp_path: Path) -> None:
    parser = build_parser()
    base = [
        "sweep",
        "--runs-root",
        str(tmp_path),
        "--agent-dir",
        str(REPO_ROOT / "spike" / "agent"),
    ]
    args = parser.parse_args(base)
    assert config_from_args(args).challenge_count == 3
    for extra in (["--parallel", "4"], ["--count", "0"], ["--parallel", "3", "--count", "1"],
                  ["--round-seconds", "1"]):
        with pytest.raises(SpikeError):
            config_from_args(parser.parse_args(base + extra))
    with pytest.raises(SpikeError):
        config_from_args(
            parser.parse_args(base + ["--knowledge-root", str(tmp_path / "missing")])
        )
    with pytest.raises(SpikeError):
        config_from_args(
            parser.parse_args(
                [
                    "sweep",
                    "--runs-root",
                    str(tmp_path),
                    "--agent-dir",
                    str(tmp_path / "no-agent"),
                ]
            )
        )


def test_run_task_stops_on_an_accepted_flag_and_records_cross_session_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = {
        "s1": [(["fact: port 80 open | evidence: nmap | confidence: high"], [])],
        "s2": [(["fact: port 80 open | evidence: curl | confidence: high"], [])],
        "s3": [(["flag: flag{accepted}"], [])],
    }
    _install_fakes(monkeypatch, scripts)
    client = FakeClient(verdicts={"flag{accepted}": True})
    config = _config(tmp_path)
    challenge = Challenge(code="WEB-1", description="find the flag", difficulty="easy")

    outcome = asyncio.run(run_task(client=client, challenge=challenge, config=config))

    assert outcome.status == "achieved"
    assert outcome.exit_reason == "flag_accepted"
    assert outcome.rounds == 2 and outcome.wrote_run
    assert client.submitted == ["flag{accepted}"]
    assert client.closed == ["WEB-1"]
    assert outcome.goal["root"]["status"] == "achieved"

    layout = RunLayout(root=config.runs_root / "WEB-1", code="WEB-1")
    result = json.loads(layout.result_path.read_text(encoding="utf-8"))
    assert result["status"] == "achieved"
    assert result["flags"][0]["accepted"] is True
    assert len(result["sessions"]) == 3
    crossed = result["blackboard"]["cross_session_facts"]
    assert crossed and crossed[0]["sessions"] == ["s1", "s2"]
    assert result["blackboard"]["rounds_rendered"] >= 2
    assert (layout.shared_dir / "intent-fact-graph.md").exists()
    assert list(layout.snapshots_dir.iterdir())


def test_run_task_requires_peer_corroboration_and_abandons_at_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = {
        "s1": [
            (
                ["fact: port 80 open | evidence: nmap | confidence: high"],
                [{"op": "propose", "id": "sg-1", "description": "enumerate the web app"}],
            )
        ],
        "s2": [(["fact: port 80 open | evidence: curl | confidence: high"], [])],
        "s3": [(["note: standing by"], [])],
    }
    _install_fakes(monkeypatch, scripts)
    client = FakeClient()
    config = _config(tmp_path, wall_time=0.05)
    challenge = Challenge(code="WEB-2", description="find the flag", difficulty="easy")

    outcome = asyncio.run(run_task(client=client, challenge=challenge, config=config))

    assert outcome.status == "abandoned"
    assert outcome.exit_reason == "window_elapsed"
    assert client.closed == ["WEB-2"]
    layout = RunLayout(root=config.runs_root / "WEB-2", code="WEB-2")
    records = [
        json.loads(line)
        for line in layout.goal_journal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        record.get("result") == "applied"
        and isinstance(record.get("goal"), dict)
        and record["goal"].get("id") == "sg-1"
        for record in records
    )
    result = json.loads(layout.result_path.read_text(encoding="utf-8"))
    assert result["goal"]["root"]["status"] == "abandoned"
    assert result["goal"]["counts"]["subgoals"] == 1


def test_run_task_propagates_a_platform_wide_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fakes(monkeypatch, {name: [] for name in ("s1", "s2", "s3")})
    client = FakeClient()
    client.fail_start = PlatformUnusable(kind="vpn_unreachable", detail="no route")
    config = _config(tmp_path)
    with pytest.raises(PlatformUnusable):
        asyncio.run(
            run_task(
                client=client,
                challenge=Challenge(code="WEB-3"),
                config=config,
            )
        )
    layout = RunLayout(root=config.runs_root / "WEB-3", code="WEB-3")
    result = json.loads(layout.result_path.read_text(encoding="utf-8"))
    assert result["exit_reason"] == "platform:vpn_unreachable"
    assert client.closed == []


class FakePlatformClient:
    """Stands in for PlatformClient inside run_sweep."""

    def __init__(self, config, **kwargs: object) -> None:
        del config, kwargs
        self.events: list[dict[str, object]] = []
        self.closed: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_challenges(self) -> list[Challenge]:
        return [
            Challenge(code="HARD-1", difficulty="hard", total_score=500),
            Challenge(code="EASY-1", difficulty="easy", total_score=100),
            Challenge(code="DONE-1", difficulty="trivial", is_completed=True),
            Challenge(code="MED-1", difficulty="medium", total_score=300),
        ]

    async def start_challenge(self, code: str) -> StartedChallenge:
        return StartedChallenge(code=code, addresses=("10.0.0.1",))

    async def submit_flag(self, code: str, flag: str) -> FlagVerdict:
        del code, flag
        return FlagVerdict(accepted=False, detail="rejected")

    async def close_challenge(self, code: str) -> None:
        self.closed.append(code)

    def events_for(self, code: str) -> list[dict[str, object]]:
        del code
        return []


class ConnectError(Exception):
    """Transport-shaped failure: run_sweep must classify it and exit 3."""


class FailingPlatformClient(FakePlatformClient):
    async def list_challenges(self) -> list[Challenge]:
        raise ConnectError("no route to the control plane")


def test_run_sweep_selects_easiest_first_and_writes_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = {name: [(["note: nothing yet"], [])] for name in ("s1", "s2", "s3")}
    _install_fakes(monkeypatch, scripts)
    monkeypatch.setattr(sweep_module, "PlatformClient", FakePlatformClient)
    monkeypatch.setattr(
        sweep_module, "load_config", lambda **kwargs: type("C", (), {"base_url": "u", "token": "t"})()
    )
    config = _config(tmp_path, challenge_count=3, parallel=3, wall_time=0.05)

    report = asyncio.run(sweep_module.run_sweep(config))

    assert [outcome.code for outcome in report.outcomes] == ["EASY-1", "MED-1", "HARD-1"]
    assert report.exit_code == EXIT_OK
    manifest = json.loads((config.runs_root / "sweep.json").read_text(encoding="utf-8"))
    assert manifest["exit_code"] == EXIT_OK
    assert [entry["challenge"] for entry in manifest["selection"]] == ["EASY-1", "MED-1", "HARD-1"]
    assert len(manifest["tasks"]) == 3
    for code in ("EASY-1", "MED-1", "HARD-1"):
        assert (config.runs_root / code / "result.json").exists()


def test_run_sweep_maps_a_platform_failure_onto_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fakes(monkeypatch, {name: [] for name in ("s1", "s2", "s3")})
    monkeypatch.setattr(sweep_module, "PlatformClient", FailingPlatformClient)
    monkeypatch.setattr(
        sweep_module, "load_config", lambda **kwargs: type("C", (), {"base_url": "u", "token": "t"})()
    )
    config = _config(tmp_path, challenge_count=3)

    report = asyncio.run(sweep_module.run_sweep(config))

    assert report.platform_kind == "platform_unreachable"
    assert report.exit_code == EXIT_PLATFORM
    manifest = json.loads((config.runs_root / "sweep.json").read_text(encoding="utf-8"))
    assert manifest["platform_failure"] == "platform_unreachable"
    assert manifest["exit_code"] == EXIT_PLATFORM


def test_run_sweep_needs_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
    monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
    config = _config(tmp_path)
    report = asyncio.run(sweep_module.run_sweep(config))
    assert report.platform_kind == "missing_credentials"
    assert report.exit_code == EXIT_PLATFORM


def test_prepare_archives_a_finished_attempt_instead_of_replaying_it(tmp_path: Path) -> None:
    layout = RunLayout(root=tmp_path / "WEB-9", code="WEB-9")
    layout.prepare()
    layout.result_path.write_text('{"status": "abandoned"}\n', encoding="utf-8")
    layout.goal_journal.write_text('{"op": "old"}\n', encoding="utf-8")

    layout.prepare()

    archives = list((layout.root / "archive").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "result.json").read_text(encoding="utf-8") == '{"status": "abandoned"}\n'
    assert not layout.result_path.exists()
    assert layout.goal_journal.exists() is False or layout.goal_journal.read_text() == ""
    assert layout.graph_path.read_text(encoding="utf-8").startswith("# Intent-fact graph")
