from __future__ import annotations

import pytest
from typer.testing import CliRunner

from harness import cli


@pytest.mark.parametrize(
    "flag",
    [
        "--allow-host-agent",
        "--gateway-mode",
        "--gateway-image",
        "--gateway-runtime",
    ],
)
def test_retired_cli_options_are_rejected(flag: str) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "benchmarks/examples/hello/task.yaml",
            "--agent",
            "tests/fixtures/pi-rpc/agent.yaml",
            flag,
            "value",
        ],
    )

    assert result.exit_code == 2
    assert "No such option" in result.output or "no such option" in result.output.lower()


@pytest.mark.parametrize("flag", ["--max-unconfirmed-progress", "--hint-threshold"])
def test_retired_tsec_heuristic_options_are_rejected(flag: str) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            "tsec",
            "--agent",
            "agents/examples/pi.yaml",
            flag,
            "5",
        ],
    )

    assert result.exit_code == 2
    assert "No such option" in result.output or "no such option" in result.output.lower()
