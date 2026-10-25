from __future__ import annotations

import json
import sqlite3
import threading
import time
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


@pytest.mark.parametrize(
    ("effect_class", "side_effect", "severity"),
    [
        ("observation_effect", True, "retryable_observation"),
        ("model_effect", True, "cost_uncertain"),
        ("brokered_effect", True, "unsafe_side_effect"),
        ("lease_effect", True, "unsafe_side_effect"),
        ("lease_effect", False, "retryable_observation"),
    ],
)
def test_unknown_severity_is_derived_from_effect(
    tmp_path: Path,
    effect_class: str,
    side_effect: bool,
    severity: str,
) -> None:
    authority, lease = _authority(
        tmp_path,
        mode="authoritative",
        allowed_effects=frozenset({effect_class}),
    )
    admission = authority.admit(
        lease,
        _intent(
            effect_class=effect_class,
            side_effect=side_effect,
            idempotency_key=effect_class,
        ),
    )
    authority.mark_dispatched(admission.action_id)

    assert authority.mark_unknown(admission.action_id, "response-lost") == severity
    assert authority.snapshot().unresolved_unknown[admission.action_id] == severity


def test_unsafe_unknown_holds_reservation_until_reconciliation(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "connection-lost")

    with pytest.raises(AuthorityError):
        authority.release(admission.action_id, "blind-release")

    before = authority.snapshot()
    assert before.held.tool_calls == 1
    authority.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=1))
    after = authority.snapshot()

    assert admission.action_id not in after.unresolved_unknown
    assert after.held.tool_calls == 0
    assert after.settled.tool_calls == 1


def test_reconcile_failed_releases_and_reconcile_unknown_keeps_held(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    failed = authority.admit(lease, _intent(idempotency_key="failed"))
    authority.mark_dispatched(failed.action_id)
    authority.mark_unknown(failed.action_id, "lost")
    authority.reconcile(failed.action_id, "failed")
    assert authority.snapshot().held.tool_calls == 0

    pending = authority.admit(lease, _intent(idempotency_key="pending"))
    authority.mark_dispatched(pending.action_id)
    authority.mark_unknown(pending.action_id, "lost")
    authority.reconcile(pending.action_id, "unknown")
    snapshot = authority.snapshot()
    assert snapshot.held.tool_calls == 1
    assert snapshot.unresolved_unknown[pending.action_id] == "unsafe_side_effect"


def test_reopen_replays_actions_budget_leases_and_unknowns(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")
    before = authority.snapshot()
    authority.appender.close()

    reopened = ActionAuthority(
        SQLiteLedgerAppender(tmp_path / "ledger.db"),
        run_id="run-action",
        mode="authoritative",
    )
    assert reopened.snapshot() == before
    reopened.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=1))


def test_settle_over_reservation_records_actual_usage(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(reservation=BudgetAmounts(tool_calls=1)))
    authority.mark_dispatched(admission.action_id)
    authority.settle(admission.action_id, BudgetAmounts(tool_calls=2))

    settled = [
        record for record in authority.appender.records() if record.kind == "budget.settled"
    ]
    assert settled[-1].payload["over_reservation"] is True
    assert authority.snapshot().settled.tool_calls == 2


def test_reconcile_unknown_then_succeeded_is_append_only(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")
    authority.reconcile(admission.action_id, "unknown")

    authority.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=1))

    snapshot = authority.snapshot()
    assert admission.action_id not in snapshot.unresolved_unknown
    assert snapshot.held.tool_calls == 0
    assert snapshot.settled.tool_calls == 1


def test_terminal_reconciliation_cannot_change_outcome(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")
    authority.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=1))

    with pytest.raises(AuthorityError):
        authority.reconcile(admission.action_id, "failed")


def test_repeated_unknown_does_not_append_another_event(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")
    before = len(authority.appender.records())

    assert authority.mark_unknown(admission.action_id, "lost") == "unsafe_side_effect"
    assert len(authority.appender.records()) == before


class CountingAppender:
    def __init__(self, inner: SQLiteLedgerAppender) -> None:
        self._inner = inner
        self.path = inner.path
        self.records_calls = 0

    def append_trace_event(self, event):
        return self._inner.append_trace_event(event)

    def append_batch(self, events):
        return self._inner.append_batch(events)

    def records(self) -> list[LedgerRecord]:
        self.records_calls += 1
        return self._inner.records()

    def verify(self) -> None:
        self._inner.verify()

    def close(self) -> None:
        self._inner.close()


def test_authority_replays_once_then_updates_incrementally(tmp_path: Path) -> None:
    appender = CountingAppender(SQLiteLedgerAppender(tmp_path / "ledger.db"))
    authority = ActionAuthority(appender, run_id="run-action", mode="authoritative")
    lease = authority.issue_lease("attempt-1", frozenset({"model_effect"}))

    for index in range(3):
        admission = authority.admit(
            lease,
            _intent(
                effect_class="model_effect",
                idempotency_key=f"incremental-{index}",
                payload_sha256=f"{index:064x}",
            ),
        )
        authority.mark_dispatched(admission.action_id)
        authority.settle(admission.action_id, BudgetAmounts(tool_calls=1))

    assert appender.records_calls == 1
    assert authority.snapshot().settled.tool_calls == 3



def test_authoritative_reopen_rejects_tampered_ledger(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent())

    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        row = connection.execute(
            "SELECT payload_json FROM ledger_records WHERE event_id = ?",
            (admission.admission_event_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["scope"] = "tampered"
        connection.execute("DROP TRIGGER IF EXISTS ledger_records_no_update")
        connection.execute(
            "UPDATE ledger_records SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), admission.admission_event_id),
        )
        connection.commit()

    reopened = ActionAuthority(
        SQLiteLedgerAppender(tmp_path / "ledger.db"),
        run_id="run-action",
        mode="authoritative",
    )
    with pytest.raises(AuthorityUnavailable):
        reopened.snapshot()


def test_reopen_marks_dispatched_pending_action_unknown(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.appender.close()

    reopened = ActionAuthority(
        SQLiteLedgerAppender(tmp_path / "ledger.db"),
        run_id="run-action",
        mode="authoritative",
    )
    reopened.recover_pending()

    snapshot = reopened.snapshot()
    assert snapshot.unresolved_unknown[admission.action_id] == "unsafe_side_effect"
    assert snapshot.held.tool_calls == 1


def test_unknown_action_and_scope_are_not_re_admitted(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    intent = _intent(effect_class="brokered_effect")
    admission = authority.admit(lease, intent)
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")

    with pytest.raises(AdmissionDenied):
        authority.admit(lease, intent)
    with pytest.raises(AdmissionDenied):
        authority.admit(lease, intent.model_copy(update={"idempotency_key": "other-key"}))

    other_scope = authority.admit(
        lease,
        intent.model_copy(update={"idempotency_key": "other-scope", "scope": "target:other"}),
    )
    assert other_scope.admitted


def test_forged_lease_effects_and_stale_dispatch_are_rejected(tmp_path: Path) -> None:
    authority, observation_lease = _authority(
        tmp_path,
        mode="authoritative",
        allowed_effects=frozenset({"observation_effect"}),
    )
    forged = observation_lease.model_copy(
        update={"allowed_effects": frozenset({"brokered_effect"})}
    )
    with pytest.raises(AuthorityError):
        authority.admit(forged, _intent(effect_class="brokered_effect"))

    lease = authority.issue_lease("attempt-1", frozenset({"brokered_effect"}))
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.issue_lease("attempt-1", frozenset({"brokered_effect"}))
    with pytest.raises(AuthorityError):
        authority.mark_dispatched(admission.action_id)


def test_concurrent_admissions_cannot_overreserve(tmp_path: Path, monkeypatch) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative", limits={"tool_calls": 1})
    original_append = authority._append_events

    def slow_append(events):
        time.sleep(0.05)
        return original_append(events)

    monkeypatch.setattr(authority, "_append_events", slow_append)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def worker(key: str) -> None:
        barrier.wait()
        try:
            outcomes.append(authority.admit(lease, _intent(idempotency_key=key)))
        except AdmissionDenied as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=worker, args=(f"key-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([outcome for outcome in outcomes if hasattr(outcome, "admitted")]) == 1
    assert authority.snapshot().held.tool_calls == 1


def test_conflicting_reconciliation_actual_and_settle_after_unknown_are_rejected(
    tmp_path: Path,
) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "lost")
    authority.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=1))

    with pytest.raises(ActionConflict):
        authority.reconcile(admission.action_id, "succeeded", BudgetAmounts(tool_calls=2))

    second_root = tmp_path / "second"
    second_root.mkdir()
    second, lease2 = _authority(second_root, mode="authoritative")
    pending = second.admit(lease2, _intent(effect_class="brokered_effect"))
    second.mark_dispatched(pending.action_id)
    second.mark_unknown(pending.action_id, "lost")
    second.reconcile(pending.action_id, "unknown")
    with pytest.raises(AuthorityError):
        second.settle(pending.action_id, BudgetAmounts(tool_calls=1))


def test_authority_events_carry_attempt_envelope(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent())
    authority.mark_dispatched(admission.action_id)
    authority.settle(admission.action_id, BudgetAmounts(tool_calls=1))

    assert {record.attempt_id for record in authority.appender.records()} == {"attempt-1"}


def test_replay_rejects_foreign_run_and_orphan_settlement(tmp_path: Path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "foreign.db")
    appender.append_trace_event(
        {
            "event_id": "foreign-event",
            "run_id": "other-run",
            "type": "run.started",
            "ts": "2026-10-25T12:00:00+00:00",
            "actor": "harness",
            "data": {},
        }
    )
    foreign = ActionAuthority(appender, run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        foreign.snapshot()

    orphan_appender = SQLiteLedgerAppender(tmp_path / "orphan.db")
    orphan_appender.append_trace_event(
        {
            "event_id": "orphan-settle",
            "run_id": "run-action",
            "type": "budget.settled",
            "ts": "2026-10-25T12:00:00+00:00",
            "actor": "harness",
            "action_id": "act_missing",
            "data": {
                "schema_version": "harness/budget-reservation/v1",
                "action_id": "act_missing",
                "reservation_id": "res_missing",
                "actual": {"tool_calls": 1},
                "over_reservation": False,
            },
        }
    )
    orphan = ActionAuthority(orphan_appender, run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        orphan.snapshot()


def test_invalid_lease_is_rejected_before_append(tmp_path: Path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    authority = ActionAuthority(appender, run_id="run-action", mode="authoritative")

    with pytest.raises(AuthorityError):
        authority.issue_lease("attempt-1", frozenset({"not-an-effect"}))

    assert appender.records() == []


def test_admission_auto_recovers_pending_dispatch_before_new_work(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="authoritative")
    admission = authority.admit(lease, _intent(effect_class="brokered_effect"))
    authority.mark_dispatched(admission.action_id)
    authority.appender.close()

    reopened = ActionAuthority(
        SQLiteLedgerAppender(tmp_path / "ledger.db"),
        run_id="run-action",
        mode="authoritative",
    )
    with pytest.raises(AdmissionDenied):
        reopened.admit(
            lease,
            _intent(effect_class="brokered_effect", idempotency_key="after-recovery"),
        )

    snapshot = reopened.snapshot()
    assert snapshot.unresolved_unknown[admission.action_id] == "unsafe_side_effect"
    assert snapshot.held.tool_calls == 1


def test_concurrent_lease_issue_assigns_contiguous_epochs(tmp_path: Path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    authority = ActionAuthority(appender, run_id="run-action", mode="authoritative")
    barrier = threading.Barrier(2)
    leases: list[object] = []

    def worker() -> None:
        barrier.wait()
        leases.append(authority.issue_lease("attempt-1", frozenset({"model_effect"})))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(lease.lease_epoch for lease in leases) == [1, 2]
