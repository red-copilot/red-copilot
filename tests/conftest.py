"""Shared environment probes and fixtures for the test suite.

Docker and bubblewrap are optional on a developer host but required for the
CI-only paths. Tests that truly need them request these fixtures so a missing
runtime skips instead of failing for an unrelated reason.
"""
from __future__ import annotations

import functools
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@functools.lru_cache(maxsize=1)
def _docker_available() -> bool:
    return shutil.which("docker") is not None


@functools.lru_cache(maxsize=1)
def _bwrap_available() -> bool:
    executable = shutil.which("bwrap")
    if executable is None:
        return False
    try:
        probe = subprocess.run(
            [executable, "--ro-bind", "/", "/", "--dev", "/dev", "--", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


@pytest.fixture
def require_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is unavailable")


@pytest.fixture
def require_bwrap() -> None:
    if not _bwrap_available():
        pytest.skip("bubblewrap is unavailable or cannot sandbox")


@pytest.fixture
def make_run_dir(tmp_path: Path) -> Callable[[str], Path]:
    """Create an empty run directory under the test's temporary root."""

    def _make(name: str = "run") -> Path:
        path = tmp_path / name
        path.mkdir()
        return path

    return _make
