from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .models import BudgetSpec
from .trace import TraceRecorder

# After the soft wall-clock deadline - or after a usage ceiling is crossed -
# the Harness stops enforcing limits and steers the Agent to summarise what it
# found and what it was trying to do. The container is only killed if the Agent
# never settles within this grace window.
FINALIZE_GRACE_SECONDS = 180

# How each enforced usage dimension is read on the two accounting paths: the
# field of a live ``UsageMetrics`` snapshot, and the key a progress ledger
# charges it under. One ordered table, so a new dimension cannot leave the
# monitor and the ledger naming different quantities.
USAGE_DIMENSION_SOURCES: dict[str, tuple[str, str]] = {
    "max_tokens": ("total_tokens", "tokens"),
    "max_model_calls": ("model_calls", "model_calls"),
    "max_tool_calls": ("tool_calls", "tool_calls"),
    "max_cost_usd": ("cost_usd", "cost_usd"),
}

USAGE_DIMENSIONS = tuple(USAGE_DIMENSION_SOURCES)


def enforced_budget_limits(spec: BudgetSpec) -> dict[str, float]:
    """Return the limits the Harness enforces for a run.

    Every declared dimension is enforced softly: crossing one does not kill
    the supervised process on the spot, it opens the shared finalize grace
    window (steer to summarise, then stop) - the same semantics the wall-clock
    deadline uses. Unset dimensions stay absent.
    """
    limits: dict[str, float] = {"wall_time": float(spec.wall_time)}
    for name in USAGE_DIMENSIONS:
        value = getattr(spec, name)
        if value is not None:
            limits[name] = float(value)
    return limits


def usage_value(metrics: UsageMetrics, dimension: str) -> float:
    """Charged usage for one enforced dimension."""
    return float(getattr(metrics, USAGE_DIMENSION_SOURCES[dimension][0]))


def usage_budget_exceeded(metrics: UsageMetrics, spec: BudgetSpec) -> str | None:
    """First declared usage dimension whose charged usage reached its ceiling."""
    for dimension in USAGE_DIMENSIONS:
        limit = getattr(spec, dimension)
        if limit is not None and usage_value(metrics, dimension) >= limit:
            return dimension
    return None


def exceeded_usage_dimension(
    usage: Mapping[str, float], limits: Mapping[str, float]
) -> str | None:
    """Same scan over a usage mapping keyed the way ledgers persist it.

    Scanned in ``USAGE_DIMENSIONS`` order so every caller names the same
    dimension when two ceilings cross at once.
    """
    for dimension in USAGE_DIMENSIONS:
        limit = limits.get(dimension)
        if limit is not None and usage.get(USAGE_DIMENSION_SOURCES[dimension][1], 0.0) >= limit:
            return dimension
    return None


@dataclass
class UsageMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


def _usage_integer(data: Mapping[str, Any], key: str, default: int = 0) -> int | None:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_cost(data: Mapping[str, Any]) -> float | None:
    value = data.get("cost_usd", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and amount >= 0 else None


def charge_usage_event(
    metrics: UsageMetrics,
    event_type: str,
    data: Mapping[str, Any],
) -> bool:
    """Validate one untrusted usage event and charge ``metrics``.

    Returns ``False`` for malformed, negative or inconsistent telemetry so every
    accounting path fails closed the same way.
    """

    if event_type == "model.usage_missing":
        return False
    if event_type == "model.request":
        count = _usage_integer(data, "count", 1)
        if count is None:
            return False
        metrics.model_calls += count
    elif event_type == "model.usage":
        input_tokens = _usage_integer(data, "input_tokens")
        output_tokens = _usage_integer(data, "output_tokens")
        total_tokens = _usage_integer(
            data, "total_tokens", (input_tokens or 0) + (output_tokens or 0)
        )
        model_calls = _usage_integer(data, "model_calls")
        cost = _usage_cost(data)
        if (
            None in (input_tokens, output_tokens, total_tokens, model_calls, cost)
            or total_tokens < input_tokens + output_tokens
        ):
            return False
        metrics.input_tokens += input_tokens
        metrics.output_tokens += output_tokens
        metrics.total_tokens += total_tokens
        metrics.model_calls += model_calls
        metrics.cost_usd += cost
    elif event_type in {"tool.call", "tool.denied"}:
        count = _usage_integer(data, "count", 1)
        if count is None:
            return False
        metrics.tool_calls += count
    return True


class UsageCeiling:
    """The soft usage-ceiling edge: cross once, then open the finalize window.

    Both accounting paths - the telemetry monitor and an in-process RPC
    session charging its own events - hold one of these, so the crossing is
    detected, stamped and traced exactly the same way in both.
    """

    def __init__(self) -> None:
        self.dimension: str | None = None
        self.deadline: float | None = None

    def observe(self, metrics: UsageMetrics, spec: BudgetSpec, trace: TraceRecorder) -> None:
        """Arm the grace window the first time a declared ceiling is reached.

        Scans only until the window is armed: after that the crossing can no
        longer change the outcome.
        """
        if self.deadline is not None:
            return
        dimension = usage_budget_exceeded(metrics, spec)
        if dimension is None:
            return
        self.dimension = dimension
        self.deadline = time.monotonic() + FINALIZE_GRACE_SECONDS
        trace.emit(
            "budget.exceeded",
            data={
                "budget": dimension,
                "limit": getattr(spec, dimension),
                "observed": usage_value(metrics, dimension),
                "grace_seconds": FINALIZE_GRACE_SECONDS,
            },
        )

    def remaining(self) -> float | None:
        """Seconds left in the grace window, or ``None`` before it is armed."""
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def elapsed(self) -> bool:
        """True once an armed grace window has run out."""
        remaining = self.remaining()
        return remaining is not None and remaining <= 0
