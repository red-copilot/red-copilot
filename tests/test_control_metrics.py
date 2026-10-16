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
# The competition-window round added no module either. Measured before 15,508,
# after 15,764 (+256: the TSec token bucket, instance registry and single 429
# retry in `benchmark/tsec_client.py`; the hint steer in `solver_loop.py` with
# its adapter/bootstrap/runner/CLI wiring; and the opt-in Harness Pi extension
# install path in `pi_adapter.py`). Re-measured from the tree, not estimated.
#
# ZCP-12 then added the restart-brief metric helpers and result plumbing in
# `benchmark/runner.py`: measured before 15,764, after 15,814 (+50).
#
# Merging origin/main (4349a22, operator hint channel) brought in
# `src/harness/hints.py`, the deliberate +1 module, plus its SolverLoop wiring.
# Measured before 15,814, after 15,987 (+173). Both hint sources share one
# `solver.hint` observation absorbed through a single helper instead of two
# duplicated blocks. Re-measured from the tree, not estimated.
#
# The review follow-up added no module: the resume budget rebase in
# `orchestrator.py`, the immediate-hint dedupe in `benchmark/tsec_adapter.py`,
# the credential digest in `extract.py` and the streak-aware fold in
# `runtime/executors.py`. Measured before 15,987, after 16,023 (+36).
#
# The 2026-10-16 sweep review (BEN-05 orchestration reliability, RUN-05 run
# closure) added no module either: the platform-failure classification and
# bounded preflight gate in `benchmark/tsec_client.py` + `tsec_adapter.py`, the
# per-case dispatch queue, resume scan and sweep summary in `tsec_adapter.py`,
# provision-before-mkdir plus submission metrics in `benchmark/runner.py`, the
# submission-due steer and steer-quieting in `solver_loop.py` with its
# bootstrap wiring, `submissions_attempted` in `progress.py`, the empty-run
# sweep in `runtime/results.py`, and the `gc-runs` command in `cli.py`.
# Measured before 16,023, after 16,658 (+635). Re-measured from the tree.
#
# The second 2026-10-16 review round (BEN-06 metric coverage) added no module:
# the steer vocabulary and the offline replay helpers in `progress.py` and
# `runtime/results.py`, the delivered-steer/tool/context counters in
# `solver_loop.py`, the run-level fold in `benchmark/runner.py`, the sweep
# summary fields in `benchmark/tsec_adapter.py` and the `harness report` block
# in `cli.py`. Measured before 16,658, after 16,968 (+310). Re-measured from
# the tree, not estimated.
#
# RUN-06 (in-run pacing) added no module either: `agent_budget_lines` plus the
# duration formatter in `progress.py`, the budget lines in the focus block of
# `world/context.py`, the slow-call trace and one-shot steer in
# `solver_loop.py`, and the bash-timeout clamp in the opt-in Pi extension
# asset. Measured before 16,968, after 17,075 (+107). Re-measured from the
# tree, not estimated.
#
# RUN-07 (dead-edge quieting) added no module: the projection in
# `world/context.py` now sources semantic observations and the action tally
# from the snapshot, summarises `tool.execution` bookkeeping and drops the
# provenance blob from the Agent-facing lines. Measured before 17,075, after
# 17,127 (+52). Re-measured from the tree, not estimated.
#
# VER-05 (Harness observation contract) added no module: the typed
# `ObservationRecord` and the `unavailable` action status in `verification.py`,
# the comparison baseline and replan narrowing in `action_verifier.py`, the
# bounded observation list in `solver_loop.py`, the platform-verdict record in
# `benchmark/runner.py`, the counter in `progress.py` and the audit tally.
# Measured before 17,127, after 17,276 (+149). Re-measured from the tree, not
# estimated.
MAX_MODULES = 57
MAX_SRC_LINES = 17_276


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
