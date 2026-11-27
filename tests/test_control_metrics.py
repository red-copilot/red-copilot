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
#
# QA World durability (2026-11-25). B7 added no module. Source lines moved
# 18,443 -> 18,475: the cache write lock (RLock), validated/idempotent legacy
# import, and the fsync'd paired export publish plus `synchronous=FULL` in
# `world/sqlite.py` (+32). Re-measured.
#
# QA observability (2026-11-25). B8 added no module. Source lines moved
# 18,475 -> 18,557: structured boundary loggers plus the ACI, recovery and
# gateway warnings in the run, session, gateway and checkpoint modules (+82).
# Re-measured.
#
# QA deduplication (2026-11-25). B9 added no module and removed duplication:
# `transition_with_trace` / `settle_with_trace` in `runtime/lifecycle.py`
# (+29) replace the hand-written phase blocks in the runner (-42) and the
# orchestrator (-25). Source lines moved 18,557 -> 18,519, so the ceiling is
# ratcheted down with the tree.
#
# First-principles hardening round 1 (2026-11-26). No module added. Source
# lines moved 18,519 -> 18,613 (+94): the shared bounded, truncate-safe cursor
# read `poll_regular_chunk` in `secureio.py` (+45, consumed by the world inbox,
# submission inbox and hint cursor), hardened no-follow/bounded reads of the
# Agent-authored summary in `solver_loop.py` and `runtime/projections.py`
# (+20, including `read_regular_text_prefix`), and the gained-flag continuation
# bookkeeping comment in the TSec sweep (+29 across the test-facing surface).
# Re-measured.
#
# Norma-comparison round 1 (2026-11-28). No module added. Source lines moved
# 18,613 -> 18,679 (+66): the shared `head_tail_clip` helper in `trace.py`
# (+27, adopted from Norma's head+tail tool-output capture; failure summaries
# live at the end of output), its use in the Pi adapter excerpt, head+tail
# previews in the harness Pi extension asset, cheap `world.revision` reads on
# the per-event checkpoint paths, and the measured context-pressure restart
# trigger in `progress.py` (+39: provider-reported prompt size tracked on the
# ledger and counted as growth since the previous restart). Re-measured.
MAX_MODULES = 61
MAX_SRC_LINES = 18_679


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
