import json

from typer.testing import CliRunner

from harness.cli import app
from harness.execution import ExecutionCapabilities


def test_capabilities_report_available_backends(monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.execution.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "bwrap"} else None,
    )

    capabilities = ExecutionCapabilities.detect()

    assert capabilities.as_dict() == {
        "docker": True,
        "gvisor_runsc": False,
        "pi": True,
        "bwrap": True,
    }


def test_capabilities_default_to_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("harness.execution.shutil.which", lambda _: None)

    capabilities = ExecutionCapabilities.detect()

    assert capabilities.as_dict() == {
        "docker": False,
        "gvisor_runsc": False,
        "pi": False,
        "bwrap": False,
    }


def test_capabilities_command_reports_no_firecracker_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.execution.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "docker" else None,
    )

    result = CliRunner().invoke(app, ["capabilities"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "docker": True,
        "gvisor_runsc": False,
        "pi": True,
        "bwrap": False,
    }
    assert "firecracker" not in payload
    assert "kvm" not in payload
