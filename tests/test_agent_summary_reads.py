"""Host-side reads of the Agent-authored summary must be hardened.

The execution boundary treats every file inside the container-writable
workspace as hostile input: reads go through ``harness.secureio`` (no final
symlink, regular file only) and carry a byte bound. ``run_summary`` and
``missing_summary_sections`` both read ``agent.summary.md`` and their output
lands in run artifacts (``summary.json``, ``result.json``), so a symlinked or
oversized summary must degrade to "missing/unreadable", never leak another
file's content into an artifact or exhaust host memory.
"""

from __future__ import annotations

from pathlib import Path

from harness.runtime.projections import run_summary
from harness.solver_loop import missing_summary_sections


def _host_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "host-secret.txt"
    # Marker-bearing so a follow-the-link read would be detectable: if the
    # sections reader follows the symlink, "confirmed" and "unfinished" come
    # from this host file and the sections list comes back short.
    secret.write_text(
        "confirmed facts\nunfinished intents\nSECRET-PROVIDER-API-KEY",
        encoding="utf-8",
    )
    return secret


def test_run_summary_does_not_follow_a_symlinked_agent_report(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    secret = _host_secret(tmp_path)
    (workspace / "agent.summary.md").symlink_to(secret)

    summary = run_summary(
        world=_empty_world(tmp_path),
        progress=_progress(),
        run_dir=tmp_path,
        outcome={"status": "finished", "success": False, "failure_class": "solver_failure"},
        agent_report_path=workspace / "agent.summary.md",
    )

    # The symlink is Agent-authored input: it must read as absent, and the
    # linked host file's content must not appear anywhere in the artifact.
    assert summary["agent_report"]["present"] is False
    assert summary["agent_report"]["text"] == ""
    assert "SECRET-PROVIDER-API-KEY" not in (tmp_path / "summary.json").read_text(
        encoding="utf-8"
    )


def test_missing_summary_sections_does_not_follow_a_symlinked_report(
    tmp_path: Path,
) -> None:
    secret = _host_secret(tmp_path)
    summary_path = tmp_path / "agent.summary.md"
    summary_path.symlink_to(secret)

    # A symlinked summary is treated the same as a missing one: every
    # requested section is reported missing, and nothing is read through it.
    assert missing_summary_sections(summary_path) == [
        "confirmed",
        "unconfirmed",
        "ruled out",
        "unfinished",
    ]


def test_run_summary_bounds_an_oversized_agent_report(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    big = workspace / "agent.summary.md"
    big.write_text("A" * (300 * 1024), encoding="utf-8")

    summary = run_summary(
        world=_empty_world(tmp_path),
        progress=_progress(),
        run_dir=tmp_path,
        outcome={"status": "finished", "success": False, "failure_class": "solver_failure"},
        agent_report_path=big,
    )

    # The report is truncated to the documented MAX_AGENT_REPORT_CHARS bound;
    # a read larger than the read limit degrades to absent rather than raising.
    assert summary["agent_report"]["present"] is True
    assert len(summary["agent_report"]["text"]) <= 4000


def _empty_world(tmp_path: Path):
    from harness.world import SQLiteWorldRepository

    return SQLiteWorldRepository(tmp_path / "world.db")


def _progress():
    from harness.progress import ProgressLedger

    return ProgressLedger(budget_limits={"wall_time": 3600})
