"""Size metrics for the pruned control spine.

The prune removed open-loop subsystems (control plane, queue, worker,
coordination, OTLP export, scoring, suite, evaluation, benchmark registry).
These ceilings keep the package from silently growing back.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "harness"

# Baseline before the control-spine prune: 75 modules, 16,997 source lines.
MAX_MODULES = 64
# Raised from 14,200 for the derived focus projection (world/focus.py plus its
# context and planner wiring), which narrows the Agent working set per round.
MAX_SRC_LINES = 14_500


def _module_names() -> set[str]:
    names: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        parts = list(path.relative_to(PACKAGE_ROOT).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return names


def _total_lines() -> int:
    return sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE_ROOT.rglob("*.py")
    )


def test_package_module_count_is_lean() -> None:
    assert len(_module_names()) <= MAX_MODULES


def test_package_source_lines_are_lean() -> None:
    assert _total_lines() <= MAX_SRC_LINES
