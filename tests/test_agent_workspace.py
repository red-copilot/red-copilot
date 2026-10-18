"""The container workspace contract: cap-dropped root runtime, harness-owned tree."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from harness.agent_workspace import (
    create_agent_workspace,
    prepare_agent_workspace_for_container,
    sync_agent_workspace,
)


def _modes(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_prepare_returns_the_root_container_identity(tmp_path: Path) -> None:
    workspace = create_agent_workspace(tmp_path)

    assert prepare_agent_workspace_for_container(workspace) == (0, 0)
    # Owner-only: the container reaches the tree through its baseline
    # capabilities rather than through world-readable permission bits.
    assert _modes(workspace) == 0o700
    assert _modes(workspace / "notes") == 0o700


def test_prepare_keeps_workspace_writable_for_a_non_root_harness(tmp_path: Path) -> None:
    workspace = create_agent_workspace(tmp_path)
    notes = workspace / "notes"
    note = notes / "notes.md"
    note.write_text("agent note\n", encoding="utf-8")
    note.chmod(0o600)

    prepare_agent_workspace_for_container(workspace)

    # The Agent writes artifacts as root; the harness identity must still own
    # the tree and be able to read those files back.
    assert workspace.stat().st_uid == os.getuid()
    assert _modes(note) == 0o600
    assert note.read_text(encoding="utf-8") == "agent note\n"


def test_prepare_preserves_executable_bits_and_skips_symlinks(tmp_path: Path) -> None:
    workspace = create_agent_workspace(tmp_path)
    script = workspace / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    link = workspace / "notes" / "alias.md"
    link.symlink_to("/etc/passwd")

    prepare_agent_workspace_for_container(workspace)

    assert _modes(script) == 0o700
    assert link.is_symlink()


def test_prepare_rejects_a_non_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    workspace.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError):
        prepare_agent_workspace_for_container(workspace)


def test_sync_agent_workspace_keeps_root_written_views_readable(tmp_path: Path) -> None:
    workspace = create_agent_workspace(tmp_path)
    prepare_agent_workspace_for_container(workspace)
    (tmp_path / "world.context.txt").write_text("facts\n", encoding="utf-8")

    sync_agent_workspace(run_dir=tmp_path, workspace=workspace)

    rendered = workspace / "world.context.txt"
    assert rendered.read_text(encoding="utf-8") == "facts\n"
    assert _modes(rendered) == 0o644


def _attempt(root: Path, *, summary: str | None, notes: dict[str, str]) -> Path:
    run_dir = root / "tsec_WEB-001_deadbeef"
    workspace = run_dir / "agent-workspace"
    (workspace / "notes").mkdir(parents=True)
    for name, text in notes.items():
        (workspace / "notes" / name).write_text(text, encoding="utf-8")
    if summary is not None:
        (workspace / "agent.summary.md").write_text(summary, encoding="utf-8")
    return run_dir


def test_seed_notes_from_attempt_carries_summary_and_working_notes(tmp_path: Path) -> None:
    from harness.agent_workspace import seed_notes_from_attempt

    previous = _attempt(
        tmp_path,
        summary="attempt one: foothold at /admin, blocked on pivot",
        notes={
            "stage-ledger.md": "stage 1 done: creds admin:admin on 10.0.0.5",
            "ruled-out.md": "SSRF blocked; upload path not injectable",
        },
    )
    workspace = tmp_path / "next" / "agent-workspace"
    workspace.mkdir(parents=True)

    written = seed_notes_from_attempt(workspace=workspace, previous_run_dir=previous)

    names = sorted(path.name for path in written)
    assert "prior-summary.md" in names
    prior_notes = sorted(path.name for path in (workspace / "notes" / "prior-notes").iterdir())
    assert len(prior_notes) == 2
    assert any("stage-ledger" in name for name in prior_notes)
    assert "admin:admin" in (
        workspace / "notes" / "prior-notes" / prior_notes[0]
    ).read_text(encoding="utf-8") + (
        workspace / "notes" / "prior-notes" / prior_notes[1]
    ).read_text(encoding="utf-8")


def test_seed_notes_from_attempt_survives_a_notes_only_attempt(tmp_path: Path) -> None:
    from harness.agent_workspace import seed_notes_from_attempt

    previous = _attempt(
        tmp_path,
        summary=None,
        notes={"notes.md": "cut by budget; reverse shell on 4444 still alive"},
    )
    workspace = tmp_path / "next" / "agent-workspace"
    workspace.mkdir(parents=True)

    written = seed_notes_from_attempt(workspace=workspace, previous_run_dir=previous)

    assert written  # notes were carried even though no summary existed
    carried = " ".join(path.read_text(encoding="utf-8") for path in written)
    assert "reverse shell" in carried
