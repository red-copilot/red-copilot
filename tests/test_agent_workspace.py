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
