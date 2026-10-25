from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harness.kernel import (
    ActionAuthority,
    ActionConflict,
    ActionIntent,
    AdmissionDenied,
    AuthorityError,
    AuthorityUnavailable,
    BudgetAmounts,
    SQLiteLedgerAppender,
)
from harness.ledger import LedgerRecord

_ALL_EFFECTS = frozenset(
    {"model_effect", "brokered_effect", "observation_effect", "lease_effect"}
)


class FailingAppender:
    path = Path("unused-ledger.db")

    def append_trace_event(self, event: Mapping[str, Any]) -> LedgerRecord:
        raise RuntimeError("injected append failure")

    def append_batch(
        self, events: Sequence[Mapping[str, Any]]
    ) -> tuple[LedgerRecord, ...]:
        raise RuntimeError("injected batch failure")

    def records(self) -> list[LedgerRecord]:
        raise RuntimeError("injected replay failure")

    def verify(self) -> None:
        raise RuntimeError("injected verify failure")

    def close(self) -> None:
        return None


def _authority(
    tmp_path: Path,
    *,
    mode: str = "authoritative",
    limits: Mapping[str, float] | None = None,
    allowed_effects: frozenset[str] = _ALL_EFFECTS,
) -> tuple[ActionAuthority, Any]:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    authority = ActionAuthority(
        appender,
        run_id="run-action",
        mode=mode,
        budget_limits=limits,
        clock=lambda: datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
    )
    return authority, authority.issue_lease("attempt-1", allowed_effects)


def _intent(
    *,
    effect_class: str = "brokered_effect",
    idempotency_key: str = "idem-1",
    payload_sha256: str = "a" * 64,
    reservation: BudgetAmounts | None = None,
    side_effect: bool = True,
) -> ActionIntent:
    return ActionIntent(
        effect_class=effect_class,
        scope="target:example",
        idempotency_key=idempotency_key,
        payload_sha256=payload_sha256,
        reservation=reservation or BudgetAmounts(tool_calls=1),
        side_effect=side_effect,
    )


def test_authoritative_admission_reserves_before_dispatch_and_settles(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative", limits={"tool_calls": 5})
    intent = _intent(reservation=BudgetAmounts(tool_calls=1))

    admission = authority.admit(lease, intent)
    before = authority.snapshot()
    authority.mark_dispatched(admission.action_id)
    authority.settle(admission.action_id, BudgetAmounts(tool_calls=1))
    after = authority.snapshot()

    assert admission.admitted
    assert before.reservations[admission.reservation_id] == "held"
    assert before.held.tool_calls == 1
    assert after.reservations[admission.reservation_id] == "settled"
    assert after.held.tool_calls == 0
    assert after.settled.tool_calls == 1


def test_authoritative_budget_denial_appends_no_reservation(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative", limits={"tool_calls": 0})

    with pytest.raises(AdmissionDenied):
        authority.admit(lease, _intent(reservation=BudgetAmounts(tool_calls=1)))

    snapshot = authority.snapshot()
    assert snapshot.actions == {}
    assert snapshot.reservations == {}
    assert snapshot.held.tool_calls == 0
    assert [record.kind for record in authority.appender.records()] == [
        "lease.issued",
        "action.denied",
    ]


def test_shadow_denial_returns_admission_with_flag(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="shadow", limits={"tool_calls": 0})

    admission = authority.admit(lease, _intent(reservation=BudgetAmounts(tool_calls=1)))

    assert admission.admitted
    assert admission.shadow_would_deny
    assert not admission.authority_degraded
    assert authority.snapshot().held.tool_calls == 1


def test_duplicate_admission_is_idempotent_and_conflicting_payload_raises(
    tmp_path: Path,
) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    intent = _intent()
    first = authority.admit(lease, intent)
    second = authority.admit(lease, intent)
    changed = intent.model_copy(update={"payload_sha256": "b" * 64})

    with pytest.raises(ActionConflict):
        authority.admit(lease, changed)

    assert first.action_id == second.action_id
    assert len(authority.appender.records()) == 3


def test_stale_lease_and_disallowed_effect_are_rejected(tmp_path: Path) -> None:
    authority, old_lease = _authority(
        tmp_path,
        mode="authoritative",
        allowed_effects=frozenset({"brokered_effect"}),
    )
    current = authority.issue_lease("attempt-1", frozenset({"model_effect"}))

    with pytest.raises(AuthorityError):
        authority.admit(old_lease, _intent(effect_class="brokered_effect"))
    with pytest.raises(AuthorityError):
        authority.admit(current, _intent(effect_class="brokered_effect"))


def test_authoritative_missing_or_failing_appender_fails_closed(tmp_path: Path) -> None:
    missing = ActionAuthority(None, run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        missing.issue_lease("attempt-1", frozenset({"model_effect"}))

    failing = ActionAuthority(FailingAppender(), run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        failing.issue_lease("attempt-1", frozenset({"model_effect"}))


def test_shadow_missing_appender_returns_degraded_admission() -> None:
    authority = ActionAuthority(None, run_id="run-action", mode="shadow")
    lease = authority.issue_lease("attempt-1", frozenset({"model_effect"}))

    admission = authority.admit(lease, _intent(effect_class="model_effect"))

    assert admission.admitted
    assert admission.authority_degraded
    assert authority.snapshot().held.tool_calls == 1


def test_release_and_settle_are_idempotent_and_conflicting_settle_raises(
    tmp_path: Path,
) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    released = authority.admit(lease, _intent(idempotency_key="release"))
    authority.release(released.action_id, "pre-dispatch-failure")
    authority.release(released.action_id, "pre-dispatch-failure")

    admitted = authority.admit(lease, _intent(idempotency_key="settle"))
    authority.mark_dispatched(admitted.action_id)
    authority.settle(admitted.action_id, BudgetAmounts(tool_calls=1))
    authority.settle(admitted.action_id, BudgetAmounts(tool_calls=1))
    with pytest.raises(ActionConflict):
        authority.settle(admitted.action_id, BudgetAmounts(tool_calls=2))
