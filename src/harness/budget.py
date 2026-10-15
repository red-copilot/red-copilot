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

_USAGE_DIMENSIONS = ("max_tokens", "max_model_calls", "max_tool_calls", "max_cost_usd")


def enforced_budget_limits(spec: BudgetSpec) -> dict[str, float]:
    """Return the limits the Harness enforces for a run.

    Every declared dimension is enforced softly: crossing one does not kill
    the supervised process on the spot, it opens the shared finalize grace
    window (steer to summarise, then stop) - the same semantics the wall-clock
    deadline uses. Unset dimensions stay absent.
    """
    limits: dict[str, float] = {"wall_time": float(spec.wall_time)}
    for name in _USAGE_DIMENSIONS:
        value = getattr(spec, name)
        if value is not None:
            limits[name] = float(value)
    return limits


def _usage_value(metrics: UsageMetrics, dimension: str) -> float:
    if dimension == "max_tokens":
        return float(metrics.total_tokens)
    if dimension == "max_model_calls":
        return float(metrics.model_calls)
    if dimension == "max_tool_calls":
        return float(metrics.tool_calls)
    return float(metrics.cost_usd)


def usage_budget_exceeded(metrics: UsageMetrics, spec: BudgetSpec) -> str | None:
    """First declared usage dimension whose charged usage reached its ceiling."""
    for dimension in _USAGE_DIMENSIONS:
        limit = getattr(spec, dimension)
        if limit is not None and _usage_value(metrics, dimension) >= limit:
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
        self._usage_exceeded: str | None = None
        self._usage_deadline: float | None = None

    @property
    def usage_exceeded(self) -> str | None:
        """Usage dimension that crossed its declared ceiling, if any."""
        return self._usage_exceeded

    def usage_deadline_reached(self) -> bool:
        """True once the grace window after a usage crossing has elapsed."""
        return self._usage_deadline is not None and time.monotonic() >= self._usage_deadline

    def mark_usage_exceeded(self) -> None:
        """Report the crossed dimension as the run's exceeded budget.

        Called by supervision loops at the grace edge, so the result carries
        the exhausted dimension the same way invalid telemetry does.
        """
        if self._usage_exceeded is not None:
            self.exceeded = self._usage_exceeded

    def poll(self) -> str | None:
        if self.exceeded:
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
        exceeded_dimension = usage_budget_exceeded(self.metrics, self.budgets)
        if exceeded_dimension is not None and self._usage_deadline is None:
            self._usage_exceeded = exceeded_dimension
            self._usage_deadline = time.monotonic() + FINALIZE_GRACE_SECONDS
            self.trace.emit(
                "budget.exceeded",
                data={
                    "budget": exceeded_dimension,
                    "limit": getattr(self.budgets, exceeded_dimension),
                    "observed": _usage_value(self.metrics, exceeded_dimension),
                    "grace_seconds": FINALIZE_GRACE_SECONDS,
                },
            )

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def _account(self, event_type: str, data: dict[str, Any]) -> bool:
        return charge_usage_event(self.metrics, event_type, data)
