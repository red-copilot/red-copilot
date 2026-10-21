"""Size metrics for the pruned control spine.

The prune removed open-loop subsystems (control plane, queue, worker,
coordination, OTLP export, scoring, suite, evaluation, benchmark registry) and
later the modules no production path imported. These ceilings keep the package
from silently growing back.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "harness"

# Baselines: 75 modules / 16,997 lines before the control-spine prune, 64 /
# 18,388 after the crew prototype was wired onto the spine, then 55 / 14,130
# after the v2 cut retired the cognition scaffolding (planner, cognition,
# skills, crew, runtime.solver_profile) in favour of the agent-native,
# file-system cognition layer. PLN-04 added `runtime.executors` (the competing
# session scheduler, shared findings channel and budget shares) on top of that
# cut, which is the deliberate +1 module; the line ceiling is re-measured from
# the tree rather than estimated.
#
# The ZCP improvement round added no module: repetition interruption, session
# restarts with a carry-forward brief, the ruled-out projection, executor
# attribution and the summary-section check all live in existing modules.
# Measured before 14,770 lines, after 15,508 (+738: the restart path and the
# brief renderer, the window-relative restart trigger in `progress.py`, and the
# whole-word summary-section marker check). Re-measured from the tree rather
# than estimated.
#
# Pi-RPC-only cut: removed host CLI/Docker/pi-host adapters, JSON mode,
# process_guard and the sidecar Gateway; added the container subagent module.
# Measured 57 modules / 16,391 source lines. RUN-09 removed heuristic steering,
# automatic hint thresholds, world-echo steers and dead repetition state;
# re-measured at 57 modules / 15,890 source lines.
MAX_MODULES = 57
MAX_SRC_LINES = 15_890


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
