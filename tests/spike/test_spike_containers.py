"""Container recipe and RPC plumbing: hardening, argv hygiene, framing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from spike_swarm.containers import (
    ContainerError,
    ContainerSpec,
    EnvVar,
    JsonlFramer,
    Mount,
    PiRpcSession,
    RpcCounters,
    account_event,
    create_command,
    passthrough_env,
    pi_rpc_command,
    solver_env,
)


def _spec(*, mounts: tuple[Mount, ...] = (), environment: tuple[EnvVar, ...] = ()) -> ContainerSpec:
    return ContainerSpec(
        name="spike-WEB-1-s1",
        image="harness/pi-kali:local",
        mounts=mounts,
        environment=environment,
        user="1000:1000",
    )


def test_create_command_keeps_the_hardening() -> None:
    command = create_command(_spec())
    for flag in (
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        "4g",
        "--cpus",
        "4",
        "--pull=never",
        "--rm",
    ):
        assert flag in command, flag
    assert command[command.index("--network") + 1] == "host"
    assert command[command.index("--cap-add") + 1] == "NET_RAW"
    assert command[command.index("--user") + 1] == "1000:1000"
    assert "docker.sock" not in " ".join(command)
    image_index = command.index("harness/pi-kali:local")
    assert command[image_index + 1 :] == ["sleep", "infinity"]


def test_mounts_record_their_read_only_mode(tmp_path: Path) -> None:
    spec = _spec(
        mounts=(
            Mount(host=tmp_path / "workspace", container="/workspace"),
            Mount(host=tmp_path / "graph.md", container="/board/intent-fact-graph.md", read_only=True),
        )
    )
    command = create_command(spec)
    pairs = [command[index + 1] for index, token in enumerate(command) if token == "-v"]
    assert f"{tmp_path / 'workspace'}:/workspace:rw" in pairs
    assert f"{tmp_path / 'graph.md'}:/board/intent-fact-graph.md:ro" in pairs


def test_environment_values_are_optional_so_credentials_stay_out_of_argv() -> None:
    spec = _spec(
        environment=(
            EnvVar("OPENAI_API_KEY"),
            EnvVar("PI_OFFLINE", "1"),
        )
    )
    command = create_command(spec)
    rendered = [command[index + 1] for index, token in enumerate(command) if token == "-e"]
    assert "OPENAI_API_KEY" in rendered
    assert all("sk-" not in token for token in command)
    assert "PI_OFFLINE=1" in rendered


def test_passthrough_env_refuses_missing_and_token_like_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BENCHMARK_TOKEN", "secret")
    variables = passthrough_env(["OPENAI_API_KEY"])
    assert variables == (EnvVar("OPENAI_API_KEY"),)
    with pytest.raises(ContainerError):
        passthrough_env(["NOT_EXPORTED"])
    with pytest.raises(ContainerError):
        passthrough_env(["BENCHMARK_TOKEN"])


def test_pi_rpc_command_frames_rpc_mode_and_resume() -> None:
    command = pi_rpc_command(
        container="spike-WEB-1-s1",
        provider="openai",
        model="gpt-5.6-sol",
        thinking="high",
    )
    assert command[:4] == ["docker", "exec", "-i", "spike-WEB-1-s1"]
    assert "--mode" in command and command[command.index("--mode") + 1] == "rpc"
    for flag in (
        "--no-approve",
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-mcp",
        "--offline",
    ):
        assert flag in command, flag
    assert command[command.index("--tools") + 1] == "read,bash,edit,write"
    assert "--session" not in command

    resumed = pi_rpc_command(
        container="c",
        provider="openai",
        model="m",
        thinking=None,
        session_id="abc-123",
    )
    assert resumed[resumed.index("--session") + 1] == "abc-123"
    assert "--thinking" not in resumed


def test_jsonl_framer_splits_on_lf_only_and_keeps_the_remainder() -> None:
    framer = JsonlFramer()
    assert framer.feed('{"a":1}\n{"b":') == ['{"a":1}']
    assert framer.feed('2}\r\n') == ['{"b":2}']
    assert framer.feed("tail") == []
    assert framer.flush() == ["tail"]
    assert framer.flush() == []


def test_account_event_tracks_usage_and_tool_calls() -> None:
    counters = RpcCounters()
    account_event({"type": "tool_execution_start", "toolName": "bash"}, counters)
    account_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "usage": {"input": 10, "output": 4, "cost": {"total": 0.25}},
            },
        },
        counters,
    )
    account_event({"type": "message_end", "message": {"role": "user"}}, counters)
    assert counters.tool_calls == 1
    assert counters.model_calls == 1
    assert counters.input_tokens == 10 and counters.output_tokens == 4
    assert counters.cost_usd == 0.25


def test_session_parse_counts_malformed_lines() -> None:
    session = PiRpcSession(container="c", provider="p", model="m", thinking=None)
    assert session._parse("not json") is None
    assert session._parse('["list"]') is None
    assert session._parse('{"type":"agent_settled"}') == {"type": "agent_settled"}
    assert session.counters.malformed_lines == 2
    assert session.events_seen == 1


def test_solver_env_carries_the_spike_contract_and_pi_state() -> None:
    variables = {variable.name: variable.value for variable in solver_env(
        session_id="s2",
        challenge="WEB-1",
        objective="find the flag",
        inbox_file="/board/inbox/s2.md",
        goals_file="/board/goals/s2.jsonl",
        round_seconds=180,
        addresses=["10.0.0.1", "10.0.0.2"],
    )}
    assert variables["SPIKE_SESSION_ID"] == "s2"
    assert variables["SPIKE_TARGETS"] == "10.0.0.1 10.0.0.2"
    assert variables["SPIKE_BOARD_INBOX"] == "/board/inbox/s2.md"
    assert variables["SPIKE_BOARD_GOALS"] == "/board/goals/s2.jsonl"
    assert variables["SPIKE_GRAPH_FILE"] == "/board/intent-fact-graph.md"
    assert variables["HOME"] == "/workspace/home"
    assert variables["PI_OFFLINE"] == "1"


def test_read_event_survives_a_line_larger_than_the_reader_limit() -> None:
    async def scenario() -> tuple[dict[str, object] | None, int]:
        session = PiRpcSession(container="c", provider="p", model="m", thinking=None)
        reader = asyncio.StreamReader(limit=8)
        reader.feed_data(b"x" * 64 + b"\n")
        reader.feed_data(b'{"type":"agent_settled"}\n')
        reader.feed_eof()

        class FakeProcess:
            stdout = reader
            returncode = None

        session._process = FakeProcess()  # type: ignore[assignment]
        event = await session.read_event(timeout=1.0)
        return dict(event) if event else None, session.counters.oversized_lines

    event, oversized = asyncio.run(scenario())
    assert event == {"type": "agent_settled"}
    assert oversized == 1
