from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_ledger_baseline_reports_phase_timings() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "ledger_baseline.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--events", "50"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["event_count"] == 50
    assert set(payload) >= {
        "events_per_second",
        "append_ms",
        "verify_ms",
        "project_ms",
        "ledger_bytes",
    }
    assert payload["ledger_bytes"] > 0


def test_ledger_baseline_rejects_reused_work_dir(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "ledger_baseline.py"
    command = [
        sys.executable,
        str(script),
        "--events",
        "50",
        "--work-dir",
        str(tmp_path),
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)
    repeated = subprocess.run(command, check=False, capture_output=True, text=True)

    assert repeated.returncode != 0
    assert "already exists" in repeated.stderr
