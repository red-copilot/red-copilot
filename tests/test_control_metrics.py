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
#
# The M1 causal shadow ledger added `harness.ledger` to the control spine. The
# tree was re-measured rather than estimated: 58 modules / 16,685 source lines;
# the ledger completion path later measured 16,950. Stage 0-1 adds the explicit
# `harness.kernel` appender port and moves the trace-to-ledger edge behind it.
# Re-measured from the tree after final-review fixes: 60 modules / 16,983 source
# lines.
#
# The kernel action-authority foundation was extracted into a dedicated
# `harness.kernel.action` module after review: keeping the authority inside the
# 48-line ledger port mixed the adapter, public models and mutable state machine.
# Measured after the focused kernel tests: 61 modules / 18,065 source lines. The
# deliberate +1 module and line increase buy the first durable enforcement point
# for unknown side effects and budget reservation.
#
# QA gate hardening (2026-11-25). The B0 gate batch added no module. Source
# lines moved 18,065 -> 18,075: `secureio.open_regular_file` gained text/binary
# overloads (+20) so the newly gated mypy modules can be checked without
# ignores; the bootstrap workspace-sync lambda became a def (-1); the TSec
# operator messages moved from `print(file=sys.stderr, flush=True)` to
# `typer.echo` (-9). Re-measured from the tree rather than estimated.
#
# QA lifecycle coherence (2026-11-25). B3 added no module. Source lines moved
# 18,075 -> 18,187: `RunStatus` error/cancelled plus `settle_for_status` and
# `resolve_run_phase` (+33), the orchestrator phase trace and status-derived
# terminal settle (+25), and the runner's verifying-before-evaluation ordering,
# exception-path phase traces and status-derived settle (+54). Re-measured.
#
# QA terminal-result guarantee (2026-11-25). B4 added no module. Source lines
# moved 18,187 -> 18,390: the setup guards and shared `_base_result` helper,
# the cancellation/interrupt unwind path that persists before propagating, the
# finalization guard, and the truncated-tail trace parser in `orchestrator.py`
# (+203). Re-measured.
#
# QA recovery skew (2026-11-25). B5 added no module. Source lines moved
# 18,390 -> 18,443: `persist_control_state` (checkpoint then progress) and the
# one-event progress-ahead recovery contract in `runtime/checkpoint.py` (+53),
# plus the call-site swaps. Re-measured.
MAX_MODULES = 61
MAX_SRC_LINES = 18_443


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
