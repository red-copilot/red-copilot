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
# Raised from 15,200 for the soft-deadline finalize path and round-based
# execution: the post-run "facts + intents" summary projection, per-dimension
# budget fractions, the RPC agent-event cursor, and the ruled-out view plus
# round brief compiled from the intent-fact graph. All are small additions to
# existing modules.
# Raised to 15,800 for the round-based execution additions: the ruled-out view,
# round brief (facts/leads/frontier/repetition), command-family derivation and
# the finalize re-ask. All are small additions to existing modules.
# Raised to 16,000 for deterministic fact extraction from tool output
# (harness/extract.py plus its adapter and ACI wiring), which is what lets the
# fact/intent graph grow from what a run actually observed.
# Raised to 16,400 for the agent-session contract consolidation: the explicit
# session phase machine, AgentCapabilities/AgentRunContext (one typed parameter
# object replacing 12+ duplicated adapter kwargs), single-source typed-event
# schemas (harness/agent_events.py), and token-budget-aware context assembly.
MAX_SRC_LINES = 16_400


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
