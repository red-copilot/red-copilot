"""Narrow write port between trace recording and the causal ledger."""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..ledger import LedgerRecord, LedgerStore

AuthorityMode = Literal["shadow", "authoritative"]
ActionEffect = Literal[
    "model_effect",
    "brokered_effect",
    "observation_effect",
    "lease_effect",
]
UnknownSeverity = Literal[
    "retryable_observation",
    "cost_uncertain",
    "unsafe_side_effect",
]

_AMOUNT_FIELDS = ("model_calls", "tool_calls", "tokens", "cost_usd")


class LedgerAppender(Protocol):
    """Append-only ledger capabilities required by the trace recorder."""

    path: Path

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord: ...

    def append_batch(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[LedgerRecord, ...]: ...

    def records(self) -> list[LedgerRecord]: ...

    def verify(self) -> None: ...

    def close(self) -> None: ...


class SQLiteLedgerAppender:
    """Delegate the kernel write port to one append-only SQLite store."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._store = LedgerStore(self.path)

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord:
        return self._store.append_trace_event(event)

    def append_batch(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[LedgerRecord, ...]:
        return self._store.append_batch(events)

    def records(self) -> list[LedgerRecord]:
        return self._store.records()

    def verify(self) -> None:
        self._store.verify()

    def close(self) -> None:
        self._store.close()


class AuthorityError(RuntimeError):
    """Base error for action authority decisions."""


class AuthorityUnavailable(AuthorityError):
    """The durable authority path cannot safely make a decision."""


class AdmissionDenied(AuthorityError):
    """The action was denied before dispatch."""


class ActionConflict(AuthorityError):
    """An action identity was reused with different immutable content."""


class BudgetAmounts(BaseModel):
    """Non-negative usage dimensions reserved and settled by the kernel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)

    @field_validator("cost_usd")
    @classmethod
    def _finite_cost(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cost_usd must be finite")
        return value

    def plus(self, other: BudgetAmounts) -> BudgetAmounts:
        return BudgetAmounts(
            model_calls=self.model_calls + other.model_calls,
            tool_calls=self.tool_calls + other.tool_calls,
            tokens=self.tokens + other.tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def exceeds(self, other: BudgetAmounts) -> bool:
        return any(
            float(getattr(self, field)) > float(getattr(other, field)) for field in _AMOUNT_FIELDS
        )


class AttemptLease(BaseModel):
    """Authority granted to one attempt epoch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    lease_epoch: int = Field(ge=1)
    allowed_effects: frozenset[ActionEffect] = Field(min_length=1)


class ActionIntent(BaseModel):
    """Immutable identity and requested reservation for one action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_class: ActionEffect
    scope: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=1, max_length=512)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation: BudgetAmounts = Field(default_factory=BudgetAmounts)
    side_effect: bool = True


class Admission(BaseModel):
    """The only dispatch-facing decision returned by the authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str
    reservation_id: str
    admission_event_id: str
    admitted: bool
    shadow_would_deny: bool
    authority_degraded: bool
    deny_reason: str | None
    attempt_id: str
    lease_epoch: int


@dataclass(frozen=True)
class ActionView:
    action_id: str
    reservation_id: str
    admission_event_id: str
    attempt_id: str
    effect_class: ActionEffect
    scope: str
    idempotency_key: str
    payload_sha256: str
    reservation: BudgetAmounts
    side_effect: bool
    shadow_would_deny: bool
    dispatched: bool
    execution: str
    effect: str
    settlement: str
    unknown_severity: UnknownSeverity | None
    reconciled_outcome: str | None
    deny_reason: str | None


@dataclass(frozen=True)
class AuthoritySnapshot:
    lease_epochs: Mapping[str, int]
    actions: Mapping[str, ActionView]
    reservations: Mapping[str, str]
    held: BudgetAmounts
    settled: BudgetAmounts
    unresolved_unknown: Mapping[str, UnknownSeverity]


@dataclass
class _ActionState:
    action_id: str
    reservation_id: str
    admission_event_id: str
    attempt_id: str
    effect_class: ActionEffect
    scope: str
    idempotency_key: str
    payload_sha256: str
    reservation: BudgetAmounts
    side_effect: bool
    shadow_would_deny: bool
    dispatched: bool = False
    execution: str = "pending"
    effect: str = "unobserved"
    settlement: str = "held"
    unknown_severity: UnknownSeverity | None = None
    reconciled_outcome: str | None = None
    deny_reason: str | None = None


@dataclass
class _ReservationState:
    reservation_id: str
    action_id: str
    requested: BudgetAmounts
    state: str = "held"
    actual: BudgetAmounts | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def action_identity(run_id: str, attempt_id: str, intent: ActionIntent) -> tuple[str, str]:
    digest = _digest(
        f"{run_id}\x00{attempt_id}\x00{intent.effect_class}\x00{intent.scope}\x00"
        f"{intent.idempotency_key}"
    )[:32]
    return f"act_{digest}", f"res_{digest}"


def _event_id(kind: str, subject: str) -> str:
    return "kevt_" + _digest(kind + "\\x00" + subject)[:32]


def _sanitize_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise AuthorityError("reason must be a string")
    return reason[:512]


class ActionAuthority:
    """Durable action admission, budget reservation and reconciliation state."""

    def __init__(
        self,
        appender: LedgerAppender | None,
        *,
        run_id: str,
        mode: AuthorityMode = "shadow",
        budget_limits: Mapping[str, float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not run_id:
            raise AuthorityError("run_id is required")
        if mode not in {"shadow", "authoritative"}:
            raise AuthorityError(f"unknown authority mode: {mode}")
        self._appender = appender
        self.run_id = run_id
        self.mode = mode
        self._clock = clock or (lambda: datetime.now(UTC))
        self._budget_limits = self._validate_limits(budget_limits or {})
        self._degraded = False
        self._replay_error: BaseException | None = None
        self._reset_state()
        if appender is not None:
            try:
                self._rebuild()
            except BaseException as exc:  # noqa: BLE001 - fail-closed state is surfaced on use
                self._degraded = True
                self._replay_error = exc

    @property
    def appender(self) -> LedgerAppender | None:
        return self._appender

    @staticmethod
    def _validate_limits(limits: Mapping[str, float]) -> dict[str, float]:
        validated: dict[str, float] = {}
        for key, value in limits.items():
            if key not in _AMOUNT_FIELDS:
                raise AuthorityError(f"unknown budget limit: {key}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AuthorityError(f"budget limit {key} must be numeric")
            amount = float(value)
            if not math.isfinite(amount) or amount < 0:
                raise AuthorityError(f"budget limit {key} must be finite and non-negative")
            validated[key] = amount
        return validated

    def _reset_state(self) -> None:
        self._lease_epochs: dict[str, int] = {}
        self._actions: dict[str, _ActionState] = {}
        self._reservations: dict[str, _ReservationState] = {}
        self._denied: dict[str, str] = {}

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AuthorityError("authority clock must return timezone-aware datetimes")
        return value.astimezone(UTC).isoformat()

    def _event(
        self,
        kind: str,
        data: Mapping[str, Any],
        *,
        event_id: str,
        action_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(data)
        if attempt_id is not None:
            payload["attempt_id"] = attempt_id
        return {
            "event_id": event_id,
            "run_id": self.run_id,
            "type": kind,
            "ts": self._now(),
            "actor": "harness",
            "action_id": action_id,
            "data": payload,
        }

    def _apply_event(
        self,
        kind: str,
        data: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> None:
        if kind == "lease.issued":
            attempt = str(data.get("attempt_id") or attempt_id or "")
            epoch = int(data.get("lease_epoch", 0))
            if attempt and epoch > 0:
                self._lease_epochs[attempt] = max(self._lease_epochs.get(attempt, 0), epoch)
            return
        if kind == "action.denied":
            action_id = str(data.get("action_id") or "")
            if action_id:
                self._denied[action_id] = str(data.get("reason") or "denied")
            return
        if kind == "action.admitted":
            action_id = str(data["action_id"])
            reservation = BudgetAmounts.model_validate(data["reservation"])
            self._actions[action_id] = _ActionState(
                action_id=action_id,
                reservation_id=str(data["reservation_id"]),
                admission_event_id=str(data["admission_event_id"]),
                attempt_id=str(data.get("attempt_id") or attempt_id or ""),
                effect_class=data["effect_class"],
                scope=str(data["scope"]),
                idempotency_key=str(data["idempotency_key"]),
                payload_sha256=str(data["payload_sha256"]),
                reservation=reservation,
                side_effect=bool(data.get("side_effect", True)),
                shadow_would_deny=bool(data.get("shadow_would_deny", False)),
            )
            return
        action_id = str(data.get("action_id") or "")
        action = self._actions.get(action_id)
        if kind == "budget.reserved":
            reservation_id = str(data["reservation_id"])
            amounts = BudgetAmounts.model_validate(data["amounts"])
            self._reservations[reservation_id] = _ReservationState(
                reservation_id=reservation_id,
                action_id=action_id,
                requested=amounts,
            )
            return
        if action is None:
            return
        if kind == "action.dispatched":
            action.dispatched = True
        elif kind == "action.unknown":
            action.execution = "unknown"
            action.effect = "unknown"
            severity = data.get("severity")
            if severity in {"retryable_observation", "cost_uncertain", "unsafe_side_effect"}:
                action.unknown_severity = severity
        elif kind == "action.reconciled":
            action.reconciled_outcome = str(data.get("outcome") or "unknown")
        elif kind == "budget.settled":
            reservation = self._reservations.get(action.reservation_id)
            if reservation is not None:
                reservation.state = "settled"
                reservation.actual = BudgetAmounts.model_validate(data["actual"])
            action.settlement = "settled"
        elif kind == "budget.released":
            reservation = self._reservations.get(action.reservation_id)
            if reservation is not None:
                reservation.state = "released"
            action.settlement = "released"

    def _apply_trace_event(self, event: Mapping[str, Any]) -> None:
        data = event.get("data")
        if not isinstance(data, Mapping):
            return
        attempt_id = data.get("attempt_id") if isinstance(data.get("attempt_id"), str) else None
        self._apply_event(str(event.get("type") or ""), data, attempt_id=attempt_id)

    def _rebuild(self) -> None:
        if self._appender is None:
            return
        records = self._appender.records()
        self._reset_state()
        self._replay_error = None
        for record in records:
            self._apply_event(record.kind, record.payload, attempt_id=record.attempt_id)

    def _append_events(self, events: Sequence[Mapping[str, Any]]) -> bool:
        if self._replay_error is not None or self._appender is None:
            if self.mode == "authoritative":
                error = AuthorityUnavailable("authoritative ledger is unavailable")
                if self._replay_error is not None:
                    raise error from self._replay_error
                raise error
            self._degraded = True
            for event in events:
                self._apply_trace_event(event)
            return False
        try:
            self._appender.append_batch(events)
        except BaseException as exc:
            if self.mode == "authoritative":
                raise AuthorityUnavailable("authoritative ledger append failed") from exc
            self._degraded = True
            for event in events:
                self._apply_trace_event(event)
            return False
        try:
            self._rebuild()
        except BaseException as exc:
            if self.mode == "authoritative":
                raise AuthorityUnavailable("authoritative ledger replay failed") from exc
            self._degraded = True
            self._reset_state()
            for event in events:
                self._apply_trace_event(event)
            return False
        return True

    def issue_lease(
        self,
        attempt_id: str,
        allowed_effects: frozenset[ActionEffect],
    ) -> AttemptLease:
        if not attempt_id or not allowed_effects:
            raise AuthorityError("lease requires an attempt and at least one effect")
        epoch = self._lease_epochs.get(attempt_id, 0) + 1
        event = self._event(
            "lease.issued",
            {
                "schema_version": "harness/lease/v1",
                "attempt_id": attempt_id,
                "lease_epoch": epoch,
                "allowed_effects": sorted(allowed_effects),
            },
            event_id=_event_id("lease.issued", f"{attempt_id}\0{epoch}"),
            attempt_id=attempt_id,
        )
        self._append_events([event])
        return AttemptLease(
            run_id=self.run_id,
            attempt_id=attempt_id,
            lease_epoch=epoch,
            allowed_effects=allowed_effects,
        )

    def _would_deny(self, snapshot: AuthoritySnapshot, requested: BudgetAmounts) -> str | None:
        for field in _AMOUNT_FIELDS:
            limit = self._budget_limits.get(field)
            if limit is None:
                continue
            projected = (
                float(getattr(snapshot.settled, field))
                + float(getattr(snapshot.held, field))
                + float(getattr(requested, field))
            )
            if projected > limit:
                return f"budget_exceeded:{field}"
        return None

    def _admission_from_state(self, state: _ActionState) -> Admission:
        return Admission(
            action_id=state.action_id,
            reservation_id=state.reservation_id,
            admission_event_id=state.admission_event_id,
            admitted=True,
            shadow_would_deny=state.shadow_would_deny,
            authority_degraded=self._degraded,
            deny_reason=None,
            attempt_id=state.attempt_id,
            lease_epoch=self._lease_epochs.get(state.attempt_id, 0),
        )

    def admit(self, lease: AttemptLease, intent: ActionIntent) -> Admission:
        if lease.run_id != self.run_id:
            raise AuthorityError("lease belongs to another run")
        if self._lease_epochs.get(lease.attempt_id) != lease.lease_epoch:
            raise AuthorityError("lease epoch is stale")
        if intent.effect_class not in lease.allowed_effects:
            raise AuthorityError("effect class is not allowed by the lease")

        action_id, reservation_id = action_identity(self.run_id, lease.attempt_id, intent)
        if action_id in self._denied:
            raise AdmissionDenied(self._denied[action_id])
        snapshot = self.snapshot()
        existing = snapshot.actions.get(action_id)
        if existing is not None:
            same = (
                existing.attempt_id == lease.attempt_id
                and existing.effect_class == intent.effect_class
                and existing.scope == intent.scope
                and existing.idempotency_key == intent.idempotency_key
                and existing.payload_sha256 == intent.payload_sha256
                and existing.reservation == intent.reservation
                and existing.side_effect == intent.side_effect
            )
            if not same:
                raise ActionConflict("action identity was reused with different content")
            return self._admission_from_state(self._actions[action_id])

        denial = self._would_deny(snapshot, intent.reservation)
        admission_event_id = _event_id("action.admitted", f"{action_id}\0{intent.payload_sha256}")
        base = {
            "schema_version": "harness/action/v1",
            "action_id": action_id,
            "reservation_id": reservation_id,
            "admission_event_id": admission_event_id,
            "attempt_id": lease.attempt_id,
            "lease_epoch": lease.lease_epoch,
            "effect_class": intent.effect_class,
            "scope": intent.scope,
            "idempotency_key": intent.idempotency_key,
            "payload_sha256": intent.payload_sha256,
            "reservation": intent.reservation.model_dump(),
            "side_effect": intent.side_effect,
            "shadow_would_deny": bool(denial),
        }
        if denial is not None and self.mode == "authoritative":
            denied = self._event(
                "action.denied",
                {
                    **base,
                    "reason": denial,
                    "schema_version": "harness/action/v1",
                },
                event_id=_event_id("action.denied", f"{action_id}\0{denial}"),
                action_id=action_id,
                attempt_id=lease.attempt_id,
            )
            self._append_events([denied])
            raise AdmissionDenied(denial)

        admitted = self._event(
            "action.admitted",
            base,
            event_id=admission_event_id,
            action_id=action_id,
            attempt_id=lease.attempt_id,
        )
        reserved = self._event(
            "budget.reserved",
            {
                "schema_version": "harness/budget-reservation/v1",
                "action_id": action_id,
                "reservation_id": reservation_id,
                "amounts": intent.reservation.model_dump(),
            },
            event_id=_event_id("budget.reserved", f"{reservation_id}\0{action_id}"),
            action_id=action_id,
            attempt_id=lease.attempt_id,
        )
        persisted = self._append_events([admitted, reserved])
        state = self._actions.get(action_id)
        if state is None:
            raise AuthorityUnavailable("admission did not become replayable")
        admission = self._admission_from_state(state)
        return admission.model_copy(
            update={
                "shadow_would_deny": bool(denial),
                "authority_degraded": not persisted,
            }
        )

    def _require_action(self, action_id: str) -> _ActionState:
        action = self._actions.get(action_id)
        if action is None:
            raise AuthorityError(f"unknown action: {action_id}")
        return action

    def mark_dispatched(self, action_id: str) -> None:
        action = self._require_action(action_id)
        if action.dispatched:
            return
        if action.settlement != "held":
            raise AuthorityError("only held actions can be dispatched")
        event = self._event(
            "action.dispatched",
            {"schema_version": "harness/action/v1", "action_id": action_id},
            event_id=_event_id("action.dispatched", action_id),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        self._append_events([event])

    def settle(self, action_id: str, actual: BudgetAmounts) -> None:
        action = self._require_action(action_id)
        reservation = self._reservations.get(action.reservation_id)
        if action.settlement == "settled":
            if reservation is not None and reservation.actual == actual:
                return
            raise ActionConflict("action was settled with different amounts")
        if action.settlement != "held":
            raise AuthorityError("action reservation is not held")
        if not action.dispatched:
            raise AuthorityError("action must be dispatched before settlement")
        if action.unknown_severity is not None and action.reconciled_outcome is None:
            raise AuthorityError("unknown action requires explicit reconciliation")
        event = self._event(
            "budget.settled",
            {
                "schema_version": "harness/budget-reservation/v1",
                "action_id": action_id,
                "reservation_id": action.reservation_id,
                "actual": actual.model_dump(),
                "over_reservation": actual.exceeds(action.reservation),
            },
            event_id=_event_id("budget.settled", action_id),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        self._append_events([event])

    def release(self, action_id: str, reason: str) -> None:
        action = self._require_action(action_id)
        if action.settlement == "released":
            return
        if action.dispatched:
            raise AuthorityError("dispatched actions cannot be released without reconciliation")
        if action.settlement == "settled":
            raise AuthorityError("settled actions cannot be released")
        if action.unknown_severity is not None and action.reconciled_outcome is None:
            raise AuthorityError("unknown actions require reconciliation")
        event = self._event(
            "budget.released",
            {
                "schema_version": "harness/budget-reservation/v1",
                "action_id": action_id,
                "reservation_id": action.reservation_id,
                "reason": _sanitize_reason(reason),
            },
            event_id=_event_id("budget.released", action_id),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        self._append_events([event])

    @staticmethod
    def _unknown_severity(action: _ActionState) -> UnknownSeverity:
        if action.effect_class == "observation_effect":
            return "retryable_observation"
        if action.effect_class == "model_effect":
            return "cost_uncertain"
        if action.effect_class == "brokered_effect":
            return "unsafe_side_effect"
        return "unsafe_side_effect" if action.side_effect else "retryable_observation"

    def mark_unknown(self, action_id: str, reason: str) -> UnknownSeverity:
        action = self._require_action(action_id)
        if action.unknown_severity is not None and action.reconciled_outcome in {None, "unknown"}:
            return action.unknown_severity
        if action.settlement != "held":
            raise AuthorityError("unknown transition requires a held reservation")
        if not action.dispatched:
            raise AuthorityError("only dispatched actions can become unknown")
        severity = self._unknown_severity(action)
        event = self._event(
            "action.unknown",
            {
                "schema_version": "harness/action/v1",
                "action_id": action_id,
                "severity": severity,
                "reason": _sanitize_reason(reason),
            },
            event_id=_event_id("action.unknown", action_id),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        self._append_events([event])
        return severity

    def reconcile(
        self,
        action_id: str,
        outcome: Literal["succeeded", "failed", "unknown"],
        actual: BudgetAmounts | None = None,
    ) -> None:
        action = self._require_action(action_id)
        if action.unknown_severity is None:
            raise AuthorityError("only unknown actions can be reconciled")
        if action.reconciled_outcome == outcome:
            return
        if (
            action.reconciled_outcome in {"succeeded", "failed"}
            or action.settlement in {"settled", "released"}
        ):
            raise AuthorityError("action reconciliation is already terminal")
        if outcome == "unknown":
            event = self._event(
                "action.reconciled",
                {
                    "schema_version": "harness/action/v1",
                    "action_id": action_id,
                    "outcome": "unknown",
                },
                event_id=_event_id("action.reconciled", f"{action_id}\0{outcome}"),
                action_id=action_id,
                attempt_id=action.attempt_id,
            )
            self._append_events([event])
            return
        if outcome == "succeeded":
            charged = actual or action.reservation
            reconciled = self._event(
                "action.reconciled",
                {
                    "schema_version": "harness/action/v1",
                    "action_id": action_id,
                    "outcome": "succeeded",
                },
                event_id=_event_id("action.reconciled", f"{action_id}\0{outcome}"),
                action_id=action_id,
                attempt_id=action.attempt_id,
            )
            settled = self._event(
                "budget.settled",
                {
                    "schema_version": "harness/budget-reservation/v1",
                    "action_id": action_id,
                    "reservation_id": action.reservation_id,
                    "actual": charged.model_dump(),
                    "over_reservation": charged.exceeds(action.reservation),
                },
                event_id=_event_id("budget.settled", action_id),
                action_id=action_id,
                attempt_id=action.attempt_id,
            )
            self._append_events([reconciled, settled])
            return
        reconciled = self._event(
            "action.reconciled",
            {
                "schema_version": "harness/action/v1",
                "action_id": action_id,
                "outcome": "failed",
            },
            event_id=_event_id("action.reconciled", f"{action_id}\0{outcome}"),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        released = self._event(
            "budget.released",
            {
                "schema_version": "harness/budget-reservation/v1",
                "action_id": action_id,
                "reservation_id": action.reservation_id,
                "reason": "reconciled-failed",
            },
            event_id=_event_id("budget.released", action_id),
            action_id=action_id,
            attempt_id=action.attempt_id,
        )
        self._append_events([reconciled, released])

    def snapshot(self) -> AuthoritySnapshot:
        if self._replay_error is not None and self.mode == "authoritative":
            raise AuthorityUnavailable("authoritative ledger replay is unavailable")
        held = BudgetAmounts()
        settled = BudgetAmounts()
        for reservation in self._reservations.values():
            if reservation.state == "held":
                held = held.plus(reservation.requested)
            elif reservation.state == "settled" and reservation.actual is not None:
                settled = settled.plus(reservation.actual)
        actions = {
            action_id: ActionView(
                action_id=state.action_id,
                reservation_id=state.reservation_id,
                admission_event_id=state.admission_event_id,
                attempt_id=state.attempt_id,
                effect_class=state.effect_class,
                scope=state.scope,
                idempotency_key=state.idempotency_key,
                payload_sha256=state.payload_sha256,
                reservation=state.reservation,
                side_effect=state.side_effect,
                shadow_would_deny=state.shadow_would_deny,
                dispatched=state.dispatched,
                execution=state.execution,
                effect=state.effect,
                settlement=state.settlement,
                unknown_severity=state.unknown_severity,
                reconciled_outcome=state.reconciled_outcome,
                deny_reason=state.deny_reason,
            )
            for action_id, state in self._actions.items()
        }
        unknown = {
            action_id: state.unknown_severity
            for action_id, state in self._actions.items()
            if state.unknown_severity is not None
            and state.reconciled_outcome not in {"succeeded", "failed"}
        }
        reservations = {
            reservation_id: state.state for reservation_id, state in self._reservations.items()
        }
        return AuthoritySnapshot(
            lease_epochs=MappingProxyType(dict(self._lease_epochs)),
            actions=MappingProxyType(actions),
            reservations=MappingProxyType(reservations),
            held=held,
            settled=settled,
            unresolved_unknown=MappingProxyType(unknown),
        )
