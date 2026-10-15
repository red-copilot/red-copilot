from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import BudgetSpec
from .secureio import open_regular_file
from .trace import TraceRecorder

MAX_AGENT_TELEMETRY_BYTES = 64 * 1024 * 1024
MAX_AGENT_TELEMETRY_CHUNK_BYTES = 1024 * 1024
MAX_AGENT_TELEMETRY_EVENT_BYTES = 4 * 1024 * 1024
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


class BudgetMonitor:
    """Tail agent/gateway telemetry and enforce declared run budgets."""

    def __init__(
        self,
        event_file: Path,
        budgets: BudgetSpec,
        trace: TraceRecorder,
    ) -> None:
        self.event_file = event_file
        self.budgets = budgets
        self.trace = trace
        self.metrics = UsageMetrics()
        self._offset = 0
        self._remainder = b""
        self._event_file_identity: tuple[int, int] | None = None
        self.exceeded: str | None = None
        # Usage ceilings are soft: the crossing opens the finalize grace
        # window instead of stopping the supervised process on the spot.
        self.usage = UsageCeiling()

    @property
    def usage_exceeded(self) -> str | None:
        """Usage dimension that crossed its declared ceiling, if any."""
        return self.usage.dimension

    def poll(self) -> str | None:
        """Return the reason the supervised run must stop, if any.

        Tails the telemetry file and reports every declared budget: invalid
        telemetry fails closed, and a usage ceiling whose finalize grace window
        has elapsed stops the run exactly like the wall-clock deadline does.
        Supervision loops only need to act on a non-``None`` return.
        """
        if self.exceeded:
            return self.exceeded
        if self.usage.elapsed():
            self.exceeded = self.usage.dimension
            return self.exceeded

        try:
            with open_regular_file(self.event_file, "rb") as handle:
                info = os.fstat(handle.fileno())
                identity = (info.st_dev, info.st_ino)
                if self._event_file_identity is None:
                    self._event_file_identity = identity
                elif identity != self._event_file_identity or info.st_size < self._offset:
                    return self._fail_closed_telemetry("file was replaced or truncated")
                if info.st_size > MAX_AGENT_TELEMETRY_BYTES:
                    return self._fail_closed_telemetry("file exceeds the telemetry size limit")
                handle.seek(self._offset)
                chunk = handle.read(MAX_AGENT_TELEMETRY_CHUNK_BYTES)
                self._offset = handle.tell()
        except OSError:
            return self._fail_closed_telemetry("file is missing or cannot be read safely")

        if not chunk:
            return None

        data = self._remainder + chunk
        lines = data.splitlines(keepends=True)
        self._remainder = b""
        for line in lines:
            if not line.endswith((b"\n", b"\r")):
                if len(line) > MAX_AGENT_TELEMETRY_EVENT_BYTES:
                    return self._fail_closed_telemetry("record exceeds the telemetry event limit")
                self._remainder = line
                continue
            if len(line) > MAX_AGENT_TELEMETRY_EVENT_BYTES:
                return self._fail_closed_telemetry("record exceeds the telemetry event limit")
            self._consume(line.strip().decode("utf-8", errors="replace"))
            if self.exceeded:
                break
        return self.exceeded

    def _fail_closed_telemetry(self, reason: str) -> str:
        self.exceeded = "invalid_telemetry"
        self.trace.emit(
            "agent.telemetry_error",
            actor="harness",
            data={"message": f"untrusted telemetry {reason}"},
        )
        self.trace.emit(
            "budget.exceeded",
            data={"budget": self.exceeded, "reason": "untrusted telemetry could not be trusted"},
        )
        return self.exceeded

    def finish(self) -> str | None:
        self.poll()
        if self._remainder and not self.exceeded:
            self._consume(self._remainder.strip())
            self._remainder = ""
        return self.exceeded

    def _consume(self, line: str) -> None:
        if not line:
            return
        try:
            payload = json.loads(line, parse_constant=self._reject_json_constant)
            if not isinstance(payload, dict):
                raise TypeError("event must be an object")
            event_type = payload.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise TypeError("event type must be a non-empty string")
            data = payload.get("data", {})
            if not isinstance(data, dict):
                raise TypeError("event data must be an object")
        except (json.JSONDecodeError, TypeError, ValueError):
            self._fail_closed_telemetry("record is malformed")
            return

        self.trace.emit(event_type, actor="agent", data=data)
        if not self._account(event_type, data):
            self.trace.emit(
                "agent.telemetry_error",
                actor="harness",
                data={"message": "invalid or negative usage telemetry", "source_type": event_type},
            )
            self.exceeded = "invalid_telemetry"
            self.trace.emit(
                "budget.exceeded",
                data={"budget": self.exceeded, "reason": "untrusted usage telemetry was invalid"},
            )
            return
        # Declared usage ceilings are enforced softly: the crossing is traced
        # once and opens the finalize grace window; the supervision loop stops
        # the process only if the window elapses without the run finishing.
        self.usage.observe(self.metrics, self.budgets, self.trace)

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _account(self, event_type: str, data: dict[str, Any]) -> bool:
        return charge_usage_event(self.metrics, event_type, data)
