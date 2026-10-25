# Kernel Action Authority Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first durable kernel component from the approved component design: a replayable
action-admission and budget-reservation state machine that can later gate model, brokered, verifier
and lease effects.

**Architecture:** Reuse the existing single-writer `LedgerAppender` and `LedgerStore`. Extend the
appender protocol with `records()` for durable replay, then add typed authority models and
`ActionAuthority` beside the appender in `harness.kernel.appender` so this first slice does not raise
the 60-module ceiling. This slice does not wire the authority into Gateway, Orchestrator or
BenchmarkRunner.

**Tech Stack:** Python 3.11+, pydantic v2, SQLite WAL, pytest, Ruff.

**Spec:** [2026-10-25-first-principles-component-design.md](../specs/2026-10-25-first-principles-component-design.md)

## Global Constraints

- A Run has exactly one ledger writer. `ActionAuthority` appends only through one injected
  `LedgerAppender`.
- `authoritative` mode fails closed before returning a dispatch-facing admission when the ledger,
  budget, lease or schema is unusable. `shadow` mode records would-deny/degraded outcomes but does
  not block the caller.
- Unknown side effects are never automatically retried. `unsafe_side_effect` keeps its reservation
  held until explicit reconciliation and cannot produce a verified objective later.
- A repeated settle with identical values is a no-op; different values raise `ActionConflict`.
- Deterministic action IDs are `act_` + first 32 hex chars of SHA-256 over
  `run_id\0attempt_id\0effect_class\0scope\0idempotency_key`.
- Reservation IDs are `res_` + the same digest without the `act_` prefix.
- Ledger event payloads are versioned; this slice uses `harness/action/v1`,
  `harness/lease/v1` and `harness/budget-reservation/v1`.
- `harness.kernel` must not import FastAPI, Starlette, Docker, subprocess, httpx, TSec or benchmark
  adapters.
- No new Python module is added in this slice. Module count stays at 60.
- Tests are offline and use temporary SQLite ledgers. No credentials, Docker or platform access.

## Review Focus

- Duplicate admission after a crash must return the recorded decision without appending a second
  reservation; a changed payload with the same identity must fail closed.
- Budget denial and ledger failure in authoritative mode must produce zero dispatch-facing
  admission and zero upstream side effect.
- Held and settled amounts must not double count after reopen, repeated settle or reconciliation.
- Stale lease epochs and cross-attempt action attribution must be rejected.
- Unknown severity must be derived from effect class and `side_effect`, never supplied by an Agent.

## File Structure

- Modify `src/harness/kernel/appender.py`: replay port plus action/lease/reservation models,
  errors, deterministic identity helpers and `ActionAuthority`.
- Modify `src/harness/kernel/__init__.py`: export the new kernel symbols.
- Create `tests/test_kernel_action.py`: authority state-machine, budget, lease, unknown and replay
  tests.
- Modify `tests/test_kernel_appender.py`: prove `records()` delegates and preserves order.
- Modify `tests/test_control_metrics.py`: re-measure the source-line ceiling after implementation.
- Modify `docs/architecture.md`: record the action-authority foundation and its non-integrated status.
- Modify `docs/roadmap/runtime.md`: attach evidence to RUN-02 without claiming Docker/live recovery.
- This plan file: check boxes and execution evidence.

---

### Task 1: Expose durable records through the appender port

**Files:**
- Modify: `src/harness/kernel/appender.py`
- Test: `tests/test_kernel_appender.py`

**Interfaces:**
- Consumes: `harness.ledger.LedgerStore.records() -> list[LedgerRecord]`.
- Produces: `LedgerAppender.records() -> list[LedgerRecord]`.
- Produces: `SQLiteLedgerAppender.records() -> list[LedgerRecord]`.

- [x] **Step 1: Write the failing replay-port test**

Add to `tests/test_kernel_appender.py`:

```python
def test_sqlite_appender_exposes_records_in_sequence_order(tmp_path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    appender.append_trace_event(_event("evt-1"))
    appender.append_batch([_event("evt-2"), _event("evt-3")])

    records = appender.records()

    assert [record.ledger_seq for record in records] == [1, 2, 3]
    assert [record.event_id for record in records] == ["evt-1", "evt-2", "evt-3"]
    appender.close()
```

- [x] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py::test_sqlite_appender_exposes_records_in_sequence_order -q`

Expected: FAIL with `AttributeError` because the appender has no `records` method.

- [x] **Step 3: Add the replay method to the port and SQLite adapter**

In `LedgerAppender`, add:

```python
def records(self) -> list[LedgerRecord]: ...
```

In `SQLiteLedgerAppender`, add:

```python
def records(self) -> list[LedgerRecord]:
    return self._store.records()
```

- [x] **Step 4: Run the appender and ledger tests**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py tests/test_ledger.py tests/test_trace_appender.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/harness/kernel/appender.py tests/test_kernel_appender.py
git commit -m "feat(kernel): expose append-only records for replay"
```

### Task 2: Add admission, reservation, lease and settlement state

**Files:**
- Modify: `src/harness/kernel/appender.py`
- Modify: `src/harness/kernel/__init__.py`
- Test: `tests/test_kernel_action.py`

**Interfaces:**
- Produces: `AuthorityMode = Literal["shadow", "authoritative"]`
- Produces: `ActionEffect = Literal["model_effect", "brokered_effect", "observation_effect", "lease_effect"]`
- Produces frozen pydantic models:
  - `BudgetAmounts(model_calls: int = 0, tool_calls: int = 0, tokens: int = 0, cost_usd: float = 0.0)`
  - `AttemptLease(run_id: str, attempt_id: str, lease_epoch: int, allowed_effects: frozenset[ActionEffect])`
  - `ActionIntent(effect_class: ActionEffect, scope: str, idempotency_key: str, payload_sha256: str, reservation: BudgetAmounts, side_effect: bool = True)`
  - `Admission(action_id: str, reservation_id: str, admission_event_id: str, admitted: bool, shadow_would_deny: bool, authority_degraded: bool, deny_reason: str | None, attempt_id: str, lease_epoch: int)`
- Produces exceptions: `AuthorityError`, `AuthorityUnavailable`, `AdmissionDenied`, `ActionConflict`
- Produces `ActionAuthority(appender, *, run_id, mode="shadow", budget_limits=None, clock=None)` with:
  - `issue_lease(attempt_id: str, allowed_effects: frozenset[ActionEffect]) -> AttemptLease`
  - `admit(lease: AttemptLease, intent: ActionIntent) -> Admission`
  - `mark_dispatched(action_id: str) -> None`
  - `settle(action_id: str, actual: BudgetAmounts) -> None`
  - `release(action_id: str, reason: str) -> None`
  - `snapshot() -> AuthoritySnapshot`
- Produces `AuthoritySnapshot` with read-only mappings for `lease_epochs`, `actions`, `reservations`
  and aggregate `held`, `settled`, `unresolved_unknown`.

- [x] **Step 1: Write failing admission/reservation tests**

Create `tests/test_kernel_action.py` with helpers that construct a SQLite appender and an authority.
Add these tests:

```python
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
    assert [record.kind for record in authority.appender.records()] == ["lease.issued", "action.denied"]


def test_shadow_denial_returns_admission_with_flag(tmp_path: Path) -> None:
    authority, lease = _authority(tmp_path, mode="shadow", limits={"tool_calls": 0})

    admission = authority.admit(lease, _intent(reservation=BudgetAmounts(tool_calls=1)))

    assert admission.admitted
    assert admission.shadow_would_deny
    assert not admission.authority_degraded
    assert authority.snapshot().held.tool_calls == 1
```

- [x] **Step 2: Write failing duplicate, conflict, lease and failure tests**

Add:

```python
def test_duplicate_admission_is_idempotent_and_conflicting_payload_raises(tmp_path: Path) -> None:
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
    authority, old_lease = _authority(tmp_path, mode="authoritative")
    authority.issue_lease("attempt-1", frozenset({"brokered_effect"}))

    with pytest.raises(AuthorityError):
        authority.admit(old_lease, _intent(effect_class="brokered_effect"))
    with pytest.raises(AuthorityError):
        authority.admit(old_lease, _intent(effect_class="model_effect"))


def test_authoritative_missing_or_failing_appender_fails_closed(tmp_path: Path) -> None:
    missing = ActionAuthority(None, run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        missing.issue_lease("attempt-1", frozenset({"model_effect"}))

    failing = ActionAuthority(_FailingAppender(), run_id="run-action", mode="authoritative")
    with pytest.raises(AuthorityUnavailable):
        failing.issue_lease("attempt-1", frozenset({"model_effect"}))


def test_release_and_settle_are_idempotent_and_conflicting_settle_raises(tmp_path: Path) -> None:
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
```

- [x] **Step 3: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_action.py -q`

Expected: FAIL because the authority models and class do not exist.

- [x] **Step 4: Implement the models, identity helpers and authority state machine**

Implement in `src/harness/kernel/appender.py` with these rules:

1. `issue_lease` reads prior `lease.issued` events for the attempt and appends epoch + 1.
2. `admit` validates run, attempt, epoch, allowed effect, finite amounts and payload identity.
3. Authoritative admission appends `action.admitted` + `budget.reserved` in one atomic batch before
   returning; shadow denial appends both with `shadow_would_deny=true` and returns `admitted=True`.
4. Authoritative budget denial appends only `action.denied` and raises `AdmissionDenied`.
5. Missing/failing appender in authoritative mode raises `AuthorityUnavailable`; in shadow mode
   returns an admitted, degraded result without claiming persistence.
6. `mark_dispatched` requires an admitted, held action; repeated dispatch is a no-op.
7. `settle` requires dispatched and held state, charges actual amounts, and marks
   `over_reservation` when actual exceeds the reservation. Identical repeats are no-ops.
8. `release` is legal only before dispatch; released reservations no longer contribute to held.
9. `snapshot` rebuilds entirely from `appender.records()` and derives held/settled totals.
10. Every event carries `run_id`, `attempt_id` when known, deterministic `event_id`,
    `schema_version` and sanitized payload.

- [x] **Step 5: Export symbols and run the focused tests**

Add the new public symbols to `src/harness/kernel/__init__.py`. Run:

`.venv/bin/python -m pytest tests/test_kernel_action.py tests/test_kernel_appender.py -q`

Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add src/harness/kernel tests/test_kernel_action.py tests/test_kernel_appender.py
git commit -m "feat(kernel): add action admission and budget reservation"
```

### Task 3: Add unknown severity, reconciliation and replay coverage

**Files:**
- Modify: `src/harness/kernel/appender.py`
- Test: `tests/test_kernel_action.py`

**Interfaces:**
- Produces: `UnknownSeverity = Literal["retryable_observation", "cost_uncertain", "unsafe_side_effect"]`
- Extends `ActionAuthority` with:
  - `mark_unknown(action_id: str, reason: str) -> UnknownSeverity`
  - `reconcile(action_id: str, outcome: Literal["succeeded", "failed", "unknown"], actual: BudgetAmounts | None = None) -> None`
- Extends `AuthoritySnapshot.unresolved_unknown: Mapping[str, UnknownSeverity]`.

- [x] **Step 1: Write failing unknown and reconciliation tests**

Add:

```python
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
    authority, lease = _authority(tmp_path, mode="authoritative", allowed_effects=frozenset({effect_class}))
    admission = authority.admit(
        lease,
        _intent(effect_class=effect_class, side_effect=side_effect, idempotency_key=effect_class),
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
```

- [x] **Step 2: Run the unknown tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_action.py -q`

Expected: FAIL because `mark_unknown` and `reconcile` do not exist.

- [x] **Step 3: Implement unknown and reconciliation transitions**

Rules:

1. Severity is derived from effect class and `side_effect`; caller/Agent cannot override it.
2. `mark_unknown` requires `dispatched=True`, `settlement=held` and no prior reconcile.
3. `release` rejects unknown reservations and requires reconciliation.
4. `reconcile("succeeded")` atomically appends reconciled + settled with actual or full reservation.
5. `reconcile("failed")` atomically appends reconciled + released.
6. `reconcile("unknown")` keeps the reservation held and remains in `unresolved_unknown`.
7. Repeating an identical unknown or reconcile operation is a no-op.

- [x] **Step 4: Run the full kernel suite**

Run: `.venv/bin/python -m pytest tests/test_kernel_action.py tests/test_kernel_appender.py tests/test_ledger.py tests/test_trace_appender.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/harness/kernel/appender.py tests/test_kernel_action.py
git commit -m "feat(kernel): reconcile unknown actions without replay"
```

### Task 4: Record boundaries, metrics and roadmap evidence

**Files:**
- Modify: `tests/test_control_metrics.py`
- Modify: `docs/architecture.md`
- Modify: `docs/roadmap/runtime.md`
- Modify: `docs/superpowers/plans/2026-10-25-kernel-action-authority.md`

**Interfaces:**
- Consumes: passing kernel tests from Tasks 1-3.
- Produces: measured source-line ceiling and documentation that says the authority is implemented but not
  wired into production dispatch yet.

- [x] **Step 1: Re-measure the package**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
root = Path("src/harness")
print(len(list(root.rglob("*.py"))))
print(sum(len(p.read_text(encoding="utf-8").splitlines()) for p in root.rglob("*.py")))
PY
```

Record the exact numbers. Update `MAX_SRC_LINES` in `tests/test_control_metrics.py` to the measured
value; do not change `MAX_MODULES` from 60.

- [x] **Step 2: Document the non-integrated foundation**

Add a short note to `docs/architecture.md` and the RUN-02 state in `docs/roadmap/runtime.md`:

```text
The kernel action authority now models admission, reservation, dispatch, settlement, unknown
severity and reconciliation over the append-only ledger. Gateway, Orchestrator and BenchmarkRunner
do not consume it yet; v2 remains the production path.
```

- [x] **Step 3: Run architecture, metrics and docs checks**

Run:

`.venv/bin/python -m pytest tests/test_architecture.py tests/test_control_metrics.py tests/test_docs_consistency.py -q`

Expected: PASS.

- [x] **Step 4: Commit**

```bash
git add tests/test_control_metrics.py docs/architecture.md docs/roadmap/runtime.md docs/superpowers/plans/2026-10-25-kernel-action-authority.md
git commit -m "docs(kernel): record action authority evidence"
```

### Final Verification

- [x] Run `.venv/bin/ruff check .`
- [x] Run `.venv/bin/python -m pytest -q`
- [x] Confirm `git diff --check`
- [x] Confirm the branch is clean
- [x] Record the full-suite count and any environment-dependent skips in the plan and final response


## Execution Evidence

- `0114960 feat(kernel): expose append-only records for replay`
- `58d46f3 feat(kernel): add action admission and budget reservation`
- `be02303 feat(kernel): reconcile unknown actions without replay`
- Focused kernel matrix: 47 passed.
- Architecture/metrics/docs matrix: 16 passed.
- Full suite: 554 passed, 1 existing Starlette/httpx deprecation warning.
- `ruff check .` and `git diff --check`: clean.
- Package remains at 60 modules; source lines re-measured to 17,805. `MAX_SRC_LINES` was raised
  from 16,983 to 17,805 because this is the first durable action-authority enforcement point.
- No production Gateway/Orchestrator/BenchmarkRunner wiring was added in this slice.
