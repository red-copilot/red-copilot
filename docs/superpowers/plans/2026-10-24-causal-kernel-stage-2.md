# Causal Kernel Stage 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every covered cross-host action behind a durable, replayable admission decision and
budget reservation before dispatch, while keeping v2 as the authoritative read path.

**Architecture:** Add one dependency-light `ActionAuthority` state machine beside the existing
`LedgerAppender`. The authority atomically appends admission plus reservation records, exposes
dispatch/settle/release/unknown transitions, and rebuilds its state from the same single ledger.
The Gateway covers model and Gateway-tool effects; benchmark submission/evaluation and task
verification use the same authority as brokered effects. Pi shell/file actions use an Attempt lease
and receive action identity after observation, because Pi has no per-command pre-dispatch hook.

**Tech Stack:** Python 3.11+, pydantic models, SQLite WAL, pytest, FastAPI/httpx test transports,
Ruff, existing `harness.ledger`, `harness.kernel`, Gateway and benchmark adapters.

**Spec:** [2026-10-23-causal-kernel-incremental-refactor-design.md](../specs/2026-10-23-causal-kernel-incremental-refactor-design.md)

## Global Constraints

- v2 remains authoritative for Run status, World, Context, Progress, Result and Checkpoint reads.
  This plan writes causal facts beside v2; it does not start Stage 3 or Stage 4.
- Default authority mode is `shadow`; `authoritative` is explicit. Shadow records whether it would
  deny but preserves v2 behavior. Authoritative fails closed before a covered action is dispatched.
- Every covered Run has exactly one `LedgerAppender` and one `ActionAuthority` sharing it. Do not
  open a second SQLite writer to the same ledger.
- The kernel package must not import FastAPI, Starlette, Docker, subprocess, httpx, TSec or benchmark
  adapters. Adapter composition stays in Gateway, Orchestrator and BenchmarkRunner.
- A Run-bound Gateway is the only authoritative model path in this stage. Standalone
  `harness gateway` and direct-provider benchmark sessions remain explicitly legacy and are not
  claimed as admitted model traffic. Do not silently describe them as covered.
- `lease_effect` actions are not pre-approved. A Pi tool call may cross the soft tool-call pool once;
  the Harness records the observed identity and stops future dispatch through the existing budget
  law. Do not claim strict zero-overshoot for container shell actions.
- Reservation settlement is idempotent. A duplicate settle with the same values is a no-op; different
  values for an already settled reservation raise `ActionConflict`.
- Unknown actions are never automatically retried or released. They remain held until an explicit
  operator reconciliation says `succeeded`, `failed` or `unknown`.
- The system still does not provide exactly-once execution or cryptographic protection against a
  malicious host administrator.
- Trace and ledger data stay sanitized. Never append credentials, raw provider errors, task secrets or
  unrestricted tool output to authority events.
- Preserve `harness/ledger-store/v1`, `harness/ledger-event/v1`, `result.json` and
  `harness/checkpoint/v2` compatibility. New semantics live in versioned payloads.
- Work offline. No Docker, model credentials or TSec platform access is required for the unit plan;
  the Docker-backed crash matrix remains an environment-dependent release gate.
- Follow Python 3.11 style, `from __future__ import annotations`, type hints on public signatures and
  Ruff's 100-column limit. Run targeted tests before the full suite.

## Coverage Boundary

- Authoritative model admission is available only for a local Run whose model traffic goes through a
  run-bound Gateway.
- BenchmarkRunner direct-provider model traffic remains legacy in Stage 2. Brokered submission and
  evaluator records can be exercised with an injected authority, but the full benchmark path stays in
  shadow mode. Do not mark Stage 2 complete for benchmark model calls until a run-bound Gateway is
  added in a later stage.
- Standalone `harness gateway` remains an unbound compatibility entrypoint and cannot claim
  authoritative Run semantics.
- This boundary is deliberate: pretending that direct-provider traffic is admitted would violate the
  first-principles invariant and undercount model usage.

## Review Focus

- A covered upstream call must have zero side effects when authority construction, admission or
  dispatch recording fails in authoritative mode.
- A replayed or duplicated settlement/release must not change charged usage twice or reopen a
  released reservation.
- A crashed dispatch must become `unknown` rather than being silently retried under a fresh action.
- A stale lease epoch or forged attempt/session attribution must never create or charge an action in
  another Attempt.
- A shadow denial must be visible in the ledger even though v2 dispatch still proceeds.

## File Structure

Create one focused kernel unit; do not introduce an `authority/` package before Stage 5:

```text
src/harness/kernel/action.py       action/reservation models, reducer and ActionAuthority
tests/test_kernel_action.py        deterministic state-machine and atomic-batch tests
tests/test_gateway_admission.py    pre-dispatch Gateway and stream settlement tests
tests/test_authority_recovery.py   reopen, unknown reconciliation and crash-window tests
```

Modify:

```text
src/harness/kernel/__init__.py             export the new public kernel types
src/harness/kernel/appender.py             add records() for durable replay
src/harness/trace.py                       expose the shared appender to composition roots
src/harness/policy.py                      finite model reservation limits
src/harness/gateway.py                     model/tool admission and settlement
src/harness/gateway_runtime.py             pass authority and lease into the run-bound Gateway
src/harness/orchestrator.py                compose authority; gate verifier dispatch
src/harness/benchmark/runner.py             gate submission and evaluator dispatch
src/harness/solver_loop.py                 observe lease-effect tool identity
src/harness/runtime/bootstrap.py            pass authority/lease into SolverLoop
src/harness/runtime/checkpoint.py           append reconciliation before v2 mutation
tests/test_gateway.py                       preserve legacy behavior when authority is absent
tests/test_kernel_appender.py               records() delegation
tests/test_solver_loop.py                   lease-effect attribution and stale lease
tests/test_benchmark_streaming.py           brokered submission/evaluation admission
tests/test_verifier.py                      verifier admission wrapper
tests/test_architecture.py                  control-spine and forbidden-import registration
tests/test_control_metrics.py               measured module/line ceilings
docs/adr/0003-action-admission-and-reservation.md
docs/architecture.md
docs/roadmap/runtime.md
docs/roadmap/quality.md
```

## Event Contract Decisions

The top-level ledger schema stays `harness/ledger-event/v1`. Authority events use these payload
schemas:

```text
harness/lease/v1              lease.issued
harness/action/v1             action.admitted, action.denied, action.dispatched,
                              action.returned, action.failed, action.unknown,
                              action.reconciled, action.released, action.observed
harness/budget-reservation/v1 budget.reserved, budget.settled, budget.released
```

Every authority event carries `run_id`, `attempt_id`, `action_id`, `lease_epoch`, sanitized
`scope`, and deterministic `event_id`. The identity rule is:

```python
action_id = "act_" + sha256(
    "\0".join([run_id, attempt_id, effect_class, scope, idempotency_key]).encode()
).hexdigest()[:32]
reservation_id = "res_" + action_id.removeprefix("act_")
```

`payload_sha256` is stored in the admission event. Reusing the same action identity with a different
payload hash raises `ActionConflict` before another batch is appended.

## Task 1: Add the durable action/reservation state machine

**Files:**
- Create: src/harness/kernel/action.py
- Modify: src/harness/kernel/appender.py
- Modify: src/harness/kernel/__init__.py
- Test: tests/test_kernel_action.py

**Interfaces:**
- Consumes: `harness.kernel.LedgerAppender`, `harness.ledger.LedgerRecord`.
- Produces: `AuthorityMode = Literal["shadow", "authoritative"]`.
- Produces: `ActionEffect = Literal["model_effect", "brokered_effect", "verifier_effect", "lease_effect"]`.
- Produces frozen pydantic models:
  - `BudgetAmounts(model_calls: int = 0, tool_calls: int = 0, tokens: int = 0, cost_usd: float = 0.0)`
  - `AttemptLease(run_id, attempt_id, lease_epoch, allowed_effects)`
  - `ActionIntent(effect_class, scope, idempotency_key, payload_sha256, reservation)`
  - `Admission(action_id, reservation_id, admission_event_id, admitted, shadow_would_deny,
    authority_degraded, deny_reason, attempt_id, lease_epoch)`
  - `AuthoritySnapshot(actions, action_attempts, reservations, held, settled, lease_epochs,
    unresolved_unknown)`, where mappings are read-only dictionaries and action values are state names.
- Produces `ActionAuthority(appender, run_id, mode, budget_limits)` with:
  - `issue_lease(attempt_id: str, allowed_effects: frozenset[ActionEffect]) -> AttemptLease`
  - `admit(intent: ActionIntent, lease: AttemptLease) -> Admission`
  - `mark_dispatched(action_id: str) -> None`
  - `settle(action_id: str, actual: BudgetAmounts, execution_status: Literal["returned", "failed"]) -> None`
  - `mark_unknown(action_id: str, reason: str) -> None`
  - `release(action_id: str, reason: str) -> None`
  - `reconcile(action_id: str, outcome: Literal["succeeded", "failed", "unknown"], actual: BudgetAmounts | None = None) -> None`
  - `observe_lease_effect(lease: AttemptLease, action_id: str, tool: str,
    state: Literal["started", "succeeded", "failed"]) -> None`
  - `snapshot() -> AuthoritySnapshot`
- Produces exceptions `AuthorityError`, `AuthorityUnavailable`, `AdmissionDenied`, `LeaseConflict`,
  `ActionConflict`.
- Extends `LedgerAppender` with `records() -> tuple[LedgerRecord, ...]`.

- [ ] **Step 1: Write the failing appender replay test**

Append to tests/test_kernel_appender.py:

```python
def test_sqlite_appender_exposes_records_for_authority_replay(tmp_path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    appender.append_trace_event(_event("evt-1"))
    appender.append_trace_event(_event("evt-2", event_type="run.finished"))

    records = appender.records()

    assert [record.event_id for record in records] == ["evt-1", "evt-2"]
    assert records[0].payload == {"task": "kernel"}
    appender.close()
```

- [ ] **Step 2: Run it and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py::test_sqlite_appender_exposes_records_for_authority_replay -q`

Expected: FAIL because `SQLiteLedgerAppender` has no `records`.

- [ ] **Step 3: Implement records()**

Add `records(self) -> tuple[LedgerRecord, ...]` to the `LedgerAppender` protocol and
`SQLiteLedgerAppender`, delegating to `LedgerStore.records()` and converting the returned list to a
tuple. Do not add a second connection or query path.

- [ ] **Step 4: Run the appender tests**

Run: `.venv/bin/python -m pytest tests/test_kernel_appender.py tests/test_trace_appender.py -q`

Expected: PASS.

- [ ] **Step 5: Write failing authority state-machine tests**

Create tests/test_kernel_action.py with helpers that build a lease and intent. The tests must include:

```python
def test_admission_and_reservation_are_one_atomic_batch(tmp_path) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    authority = ActionAuthority(
        appender=appender,
        run_id="run-authority",
        mode="authoritative",
        budget_limits={"max_model_calls": 2, "max_tokens": 100, "max_cost_usd": 1.0},
    )
    lease = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))
    admission = authority.admit(
        ActionIntent(
            effect_class="model_effect",
            scope="model:test",
            idempotency_key="request-1",
            payload_sha256="a" * 64,
            reservation=BudgetAmounts(model_calls=1, tokens=40, cost_usd=0.2),
        ),
        lease,
    )

    kinds = [record.kind for record in appender.records()]
    assert admission.admitted is True
    assert kinds == ["lease.issued", "action.admitted", "budget.reserved"]
    assert appender.records()[1].event_id == admission.admission_event_id
    assert appender.records()[2].causation_id == admission.admission_event_id


def test_authoritative_budget_denial_appends_denial_and_raises(tmp_path) -> None:
    authority = _authority(tmp_path, max_model_calls=1)
    lease = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))
    authority.admit(_intent("first", model_calls=1), lease)

    with pytest.raises(AdmissionDenied, match="budget_exhausted"):
        authority.admit(_intent("second", model_calls=1), lease)

    records = authority.appender.records()
    assert records[-1].kind == "action.denied"
    assert records[-1].payload["deny_reason"] == "budget_exhausted"
    assert authority.snapshot().held.model_calls == 1


def test_shadow_denial_is_recorded_but_admission_is_allowed(tmp_path) -> None:
    authority = _authority(tmp_path, mode="shadow", max_model_calls=0)
    lease = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))

    admission = authority.admit(_intent("over-budget", model_calls=1), lease)

    assert admission.admitted is True
    assert admission.shadow_would_deny is True
    assert admission.deny_reason == "budget_exhausted"


def test_settle_is_idempotent_but_conflicting_settle_raises(tmp_path) -> None:
    authority = _authority(tmp_path, max_model_calls=2)
    lease = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))
    admission = authority.admit(_intent("one", model_calls=1), lease)
    authority.mark_dispatched(admission.action_id)
    authority.settle(admission.action_id, BudgetAmounts(model_calls=1), "returned")
    before = len(authority.appender.records())

    authority.settle(admission.action_id, BudgetAmounts(model_calls=1), "returned")
    assert len(authority.appender.records()) == before

    with pytest.raises(ActionConflict):
        authority.settle(admission.action_id, BudgetAmounts(model_calls=2), "returned")


def test_unknown_holds_reservation_until_explicit_reconciliation(tmp_path) -> None:
    authority = _authority(tmp_path, max_model_calls=1)
    lease = authority.issue_lease("attempt:primary", frozenset({"brokered_effect"}))
    admission = authority.admit(_intent("submit", tool_calls=1), lease)
    authority.mark_dispatched(admission.action_id)
    authority.mark_unknown(admission.action_id, "response_lost")

    assert authority.snapshot().held.tool_calls == 1
    authority.reconcile(admission.action_id, "failed")

    snapshot = authority.snapshot()
    assert snapshot.held.tool_calls == 0
    assert snapshot.actions[admission.action_id] == "reconciled"


def test_stale_lease_cannot_admit_or_charge(tmp_path) -> None:
    authority = _authority(tmp_path, max_model_calls=2)
    old = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))
    new = authority.issue_lease("attempt:primary", frozenset({"model_effect"}))

    with pytest.raises(LeaseConflict):
        authority.admit(_intent("stale", model_calls=1), old)

    admission = authority.admit(_intent("current", model_calls=1), new)
    assert admission.lease_epoch == 2
    assert [record.kind for record in authority.appender.records()].count("budget.reserved") == 1
```

- [ ] **Step 6: Run the authority tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_kernel_action.py -q`

Expected: FAIL because `harness.kernel.action` and the authority types do not exist.

- [ ] **Step 7: Implement the authority state machine**

Create src/harness/kernel/action.py. Keep it free of infrastructure imports. Implement these rules:

1. `issue_lease` appends `lease.issued`; the epoch is one greater than every prior epoch for the
   same `attempt_id` reconstructed from the ledger.
2. `admit` checks lease run/attempt/epoch/allowed effects, payload identity, and finite reservation
   amounts before writing.
3. Authoritative admission writes `action.admitted` and `budget.reserved` in one `append_batch` and
   only returns after the batch commits. A pre-existing identical batch is idempotent; conflicting
   content raises `LedgerConflict`.
4. If a finite budget limit would be crossed by `settled + held + requested`, authoritative mode
   appends `action.denied` and raises `AdmissionDenied`. Shadow mode appends `action.admitted` with
   `shadow_would_deny=true`, reserves the amount and returns an allowed admission.
5. If the appender is `None` or raises in authoritative mode, raise `AuthorityUnavailable`; no
   dispatch-facing `Admission` is returned. In shadow mode, record the degradation in memory and
   return an allowed admission with `authority_degraded=true`.
6. `mark_dispatched`, `settle`, `mark_unknown`, `release` and `reconcile` enforce the state machine,
   append deterministically named event IDs, and replay the state after every append.
7. `settle` charges the actual amount even when it exceeds the reservation, and marks the settlement
   `over_reservation=true`. This is accounting truth, not an exactly-once claim.
8. `release` is legal only before dispatch or for a failed pre-dispatch attempt. Unknown reservations
   cannot be released; they require `reconcile`.
9. `reconcile("succeeded")` uses the supplied actual amount, or the full reservation when omitted.
   `reconcile("failed")` releases the reservation. `reconcile("unknown")` keeps it held.
10. `observe_lease_effect` appends `action.observed` and, only for `started`, a zero-reservation
    `budget.settled` with `tool_calls=1`. A repeat for the same action does not charge twice.

Expose `appender` as a read-only attribute on `ActionAuthority` for tests and composition roots.
Export the new types from src/harness/kernel/__init__.py.

- [ ] **Step 8: Run the authority and ledger tests**

Run: `.venv/bin/python -m pytest tests/test_kernel_action.py tests/test_kernel_appender.py tests/test_ledger.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/harness/kernel tests/test_kernel_action.py tests/test_kernel_appender.py
git commit -m "feat(kernel): add durable action admission state machine"
```

## Task 2: Gate run-bound model calls before upstream dispatch

**Files:**
- Modify: src/harness/trace.py
- Modify: src/harness/policy.py
- Modify: src/harness/gateway.py
- Modify: src/harness/gateway_runtime.py
- Modify: src/harness/orchestrator.py
- Test: tests/test_gateway_admission.py
- Test: tests/test_gateway.py

**Interfaces:**
- Consumes: `ActionAuthority`, `AttemptLease`, `ActionIntent`, `BudgetAmounts`.
- Produces: `TraceRecorder.ledger_appender` read-only property returning `LedgerAppender | None`.
- Produces: `GatewayPolicy.max_prompt_tokens` (default `32768`, minimum `1`) and
  `GatewayPolicy.default_max_output_tokens` (default `8192`, minimum `1`).
- Produces: `create_gateway_app(..., action_authority: ActionAuthority | None = None,
  action_lease: AttemptLease | None = None)`.
- Produces: `HostGatewayRuntime(..., action_authority=None, action_lease=None)` and matching
  `build_gateway_runtime` keyword arguments.
- Produces: non-stream and streaming model paths that admit, mark dispatched, dispatch, settle and
  record denials without leaking upstream text or credentials.

- [ ] **Step 1: Write failing Gateway admission tests**

Create tests/test_gateway_admission.py. Use `httpx.MockTransport`, a temporary SQLite appender and a
real `ActionAuthority`. Include these tests:

```python
def test_authoritative_model_budget_denial_never_calls_upstream(tmp_path: Path) -> None:
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_completion(usage=10))

    app, authority, lease = _authoritative_gateway(
        tmp_path,
        upstream,
        budget_limits={"max_model_calls": 0},
    )
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
    )

    assert response.status_code == 429
    assert upstream_calls == []
    assert [record.kind for record in authority.appender.records()][-1] == "action.denied"


def test_authoritative_ledger_failure_never_calls_upstream(tmp_path: Path) -> None:
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_completion(usage=10))

    app, authority, lease = _authoritative_gateway(
        tmp_path,
        upstream,
        appender=FailingAppender("ledger unavailable"),
    )
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
    )

    assert response.status_code == 503
    assert upstream_calls == []


def test_shadow_denial_is_recorded_and_upstream_still_runs(tmp_path: Path) -> None:
    upstream_calls: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200, json=_completion(usage=10))

    app, authority, lease = _shadow_gateway(
        tmp_path,
        upstream,
        budget_limits={"max_model_calls": 0},
    )
    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
    )

    assert response.status_code == 200
    assert len(upstream_calls) == 1
    admitted = [r for r in authority.appender.records() if r.kind == "action.admitted"]
    assert admitted[-1].payload["shadow_would_deny"] is True
```

Add equivalent streaming tests for successful usage settlement and disconnect-to-unknown. The
disconnect test must assert the reservation remains held and no second upstream request occurs.

- [ ] **Step 2: Run the new Gateway tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_gateway_admission.py -q`

Expected: FAIL because the Gateway does not accept or consult an authority.

- [ ] **Step 3: Expose the shared appender and compose one authority**

Add `TraceRecorder.ledger_appender` returning the current `_ledger`. In the local Orchestrator,
after trace construction and recovered-usage loading, create one `ActionAuthority` with
`mode="shadow"` by default and the Run's enforced budget limits. If authoritative mode is requested
but no appender exists, fail before environment or Gateway startup. Issue one primary lease with all
four effect classes and pass the authority and lease to `build_gateway_runtime`.

Do not let the Gateway construct its own ledger. Reuse the appender owned by the Run trace.

- [ ] **Step 4: Add finite model reservation policy**

Extend `GatewayPolicy` with `max_prompt_tokens` and `default_max_output_tokens`. Build a model
reservation from the request:

```python
output_tokens = int(payload.get("max_completion_tokens") or payload.get("max_tokens")
                    or active_policy.default_max_output_tokens)
reservation = BudgetAmounts(
    model_calls=1,
    tokens=min(active_policy.max_prompt_tokens, output_tokens) + output_tokens,
    cost_usd=pricing.cost(active_policy.max_prompt_tokens, output_tokens),
)
```

Reject non-positive, boolean or non-integer token values with HTTP 400 before admission.

- [ ] **Step 5: Implement pre-dispatch admission**

For `/v1/chat/completions`:

1. Parse the body and validate the requested output cap.
2. Build an `ActionIntent` with effect class `model_effect`, scope `model:<model>`, an idempotency
   key from a valid `Idempotency-Key` header or a fresh random key, the canonical request payload
   hash and the reservation.
3. Call `authority.admit`; map `AdmissionDenied` to HTTP 429 and `AuthorityUnavailable` to HTTP 503.
   Do not build an upstream request before admission succeeds.
4. Call `authority.mark_dispatched` immediately before the `httpx` send. If this append fails, return
   503 and do not send.
5. Include `action_id`, `attempt_id` and `lease_epoch` only in local Gateway events and trace data;
   do not forward those fields upstream.
6. On a valid usage payload, call `settle` once with the actual tokens/cost. On missing or invalid
   usage, settle the reserved amount and emit `model.usage_missing`.
7. After `mark_dispatched`, every transport failure is unknown and keeps the reservation held,
   including `httpx.ConnectError`; emit the existing `model.upstream_error`. Never auto-retry it.
8. For streams, settle in the `finally` block after the final usage line or disconnect. A client
   cancellation before response completion leaves the action unknown; do not release it.

- [ ] **Step 6: Preserve legacy Gateway behavior**

When `action_authority` is absent, keep existing behavior and record no admission claim. Add a test
that the legacy event sequence remains unchanged. Standalone `harness gateway` continues to work,
but its events must not say or imply `authoritative`.

- [ ] **Step 7: Run Gateway tests**

Run: `.venv/bin/python -m pytest tests/test_gateway_admission.py tests/test_gateway.py tests/test_pi_adapter.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/harness/trace.py src/harness/policy.py src/harness/gateway.py \
  src/harness/gateway_runtime.py src/harness/orchestrator.py \
  tests/test_gateway_admission.py tests/test_gateway.py
git commit -m "feat(gateway): admit model calls before dispatch"
```

## Task 3: Gate Gateway tools as brokered effects

**Files:**
- Modify: src/harness/gateway.py
- Test: tests/test_gateway_admission.py
- Test: tests/test_gateway.py

**Interfaces:**
- Consumes: the `ActionAuthority` and `AttemptLease` from Task 2.
- Produces: every file.read, file.write and registered adapter call admitted with
  `effect_class="brokered_effect"` and `reservation=BudgetAmounts(tool_calls=1)` before its
  filesystem or adapter side effect.
- Produces: policy/argument failures before admission; attempted operations settle one tool call;
  cancellation and generic adapter failures mark unknown.

- [ ] **Step 1: Write failing tool-admission tests**

Add to tests/test_gateway_admission.py:

```python
def test_authoritative_tool_admission_denial_does_not_execute_file_write(tmp_path: Path) -> None:
    target = tmp_path / "must-not-exist.txt"
    app, authority, lease = _authoritative_gateway(
        tmp_path,
        lambda request: httpx.Response(200),
        budget_limits={"max_tool_calls": 0},
    )

    response = TestClient(app).post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"name": "file.write", "args": {"path": target.name, "content": "x"},
              "tool_call_id": "call-denied"},
    )

    assert response.status_code == 429
    assert not target.exists()
    assert [record.kind for record in authority.appender.records()][-1] == "action.denied"


def test_registered_adapter_is_not_started_after_ledger_failure(tmp_path: Path) -> None:
    adapter = RecordingToolAdapter()
    app, authority, lease = _authoritative_gateway(
        tmp_path,
        lambda request: httpx.Response(200),
        appender=FailingAppender("ledger unavailable"),
        tool_adapters=(adapter,),
    )

    response = TestClient(app).post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"name": adapter.tool_name, "args": {}, "tool_call_id": "call-no-ledger"},
    )

    assert response.status_code == 503
    assert adapter.calls == []


def test_failing_adapter_marks_tool_unknown_and_holds_reservation(tmp_path: Path) -> None:
    adapter = FailingToolAdapter()
    app, authority, lease = _authoritative_gateway(
        tmp_path,
        lambda request: httpx.Response(200),
        tool_adapters=(adapter,),
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/tools/call",
        headers={"Authorization": "Bearer gateway-secret"},
        json={"name": adapter.tool_name, "args": {}, "tool_call_id": "call-fails"},
    )

    assert response.status_code == 502
    assert [r.kind for r in authority.appender.records()].count("action.unknown") == 1
    assert authority.snapshot().held.tool_calls == 1
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_gateway_admission.py -k tool -q`

Expected: FAIL because tool dispatch has no admission.

- [ ] **Step 3: Admit before filesystem and adapter effects**

Restructure the tool handler so it performs only policy checks, path/payload validation and call-ID
claim before admission. Then:

1. Build a brokered `ActionIntent` with the sanitized tool name and call ID as the idempotency key.
2. Call `admit` and then `mark_dispatched` before touching the file or invoking an adapter.
3. Emit `tool.denied` on admission denial and return 429.
4. On a successful operation, settle one tool call and emit the existing `tool.result`.
5. On cancellation, `PermissionError`, `ValueError`, `OSError` or a generic adapter exception, use
   the existing classification: cancellation/generic failures are unknown; validation and policy
   failures are failed. Settle an attempted call or release a pre-dispatch failure exactly once.

Include `action_id` in the existing tool call/result events. Do not change the HTTP response shape
for successful tool calls.

- [ ] **Step 4: Run the Gateway suites**

Run: `.venv/bin/python -m pytest tests/test_gateway_admission.py tests/test_gateway.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harness/gateway.py tests/test_gateway_admission.py tests/test_gateway.py
git commit -m "feat(gateway): admit host tools before execution"
```

## Task 4: Gate benchmark submissions, evaluation and task verification

**Files:**
- Modify: src/harness/orchestrator.py
- Modify: src/harness/benchmark/runner.py
- Test: tests/test_benchmark_streaming.py
- Test: tests/test_verifier.py
- Test: tests/test_orchestrator.py

**Interfaces:**
- Consumes: `ActionAuthority`, one `AttemptLease` per planned executor.
- Produces: `_evaluate_benchmark(..., authority, lease)` that admits scope
  `benchmark:evaluate:<case>` before `adapter.evaluate`.
- Produces: submission admission with idempotency key
  `sha256(case_id + "\\0" + submission.type + "\\0" + submission.value)`, before
  `adapter.submit`.
- Produces: verifier admission with scope `task_verifier:<task.id>` and a per-call idempotency key
  before `run_verifier`.
- Produces: a `submission_lock` protecting the existing run-level duplicate set so two sessions
  cannot both pass the check and both dispatch the same candidate.

- [ ] **Step 1: Write failing brokered-action tests**

Add tests that use a recording fake adapter and a real authority:

```python
def test_rejected_submission_budget_does_not_call_adapter(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    authority = _exhausted_tool_authority(tmp_path)
    lease = authority.issue_lease("attempt:executor", frozenset({"brokered_effect"}))

    outcome = _run_submission(
        tmp_path, authority, lease, adapter, Submission(type="flag", value="flag{one}")
    )

    assert outcome.admitted is False
    assert adapter.submit_calls == 0
    assert authority.appender.records()[-1].kind == "action.denied"


def test_evaluator_admission_happens_before_adapter_evaluate(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    authority = _exhausted_tool_authority(tmp_path)
    lease = authority.issue_lease("attempt:executor", frozenset({"brokered_effect"}))

    outcome = _run_evaluation(tmp_path, authority, lease, adapter, _benchmark_session())

    assert outcome.admitted is False
    assert adapter.evaluate_calls == 0
    assert authority.appender.records()[-1].kind == "action.denied"


def test_verifier_admission_denial_skips_subprocess_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[object] = []

    def forbidden_verifier(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("verifier must not dispatch")

    monkeypatch.setattr("harness.orchestrator.run_verifier", forbidden_verifier)
    authority = _exhausted_tool_authority(tmp_path)
    lease = authority.issue_lease("attempt:primary", frozenset({"verifier_effect"}))

    outcome = _run_task_verifier(tmp_path, authority, lease, _task_spec(), seed=0)

    assert outcome.admitted is False
    assert calls == []
    assert authority.appender.records()[-1].kind == "action.denied"
```

Also add:

```python
def test_submission_unknown_is_not_retried_after_adapter_raises(tmp_path: Path) -> None:
    adapter = RaiseAfterSendAdapter()
    authority = _fresh_tool_authority(tmp_path)
    lease = authority.issue_lease("attempt:executor", frozenset({"brokered_effect"}))
    submission = Submission(type="flag", value="flag{once}")

    first = _run_submission(tmp_path, authority, lease, adapter, submission)
    second = _run_submission(tmp_path, authority, lease, adapter, submission)

    assert first.admitted is True
    assert first.unknown is True
    assert second.admitted is False
    assert adapter.submit_calls == 1
    assert [r.kind for r in authority.appender.records()].count("action.unknown") == 1
```

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_benchmark_streaming.py tests/test_verifier.py tests/test_orchestrator.py -k 'admission or settle' -q`

Expected: FAIL because the adapters and verifier are called without admission.

- [ ] **Step 3: Add attempt leases to benchmark sessions**

Issue one lease per `ExecutorSessionPlan` after the shared authority exists. Store it on the existing
per-session runtime object. The stable attempt identity is the executor's declared `executor_id`; a
context restart keeps the same attempt and lease. The local single-session path uses
`attempt:primary`.

- [ ] **Step 4: Gate submissions and evaluators**

Wire the submission path as follows:

1. Acquire `submission_lock`.
2. Check the existing candidate set.
3. Build and admit a `brokered_effect` intent.
4. Add the candidate to the duplicate set only after admission succeeds.
5. `mark_dispatched`, call `adapter.submit`, settle on a normal return and mark unknown on an
   exception.
6. Preserve the existing rule that accepted submission feedback is not objective success.

For `_evaluate_benchmark`, admit through the primary lease, mark dispatched, call
`adapter.evaluate`, settle one tool call and mark unknown on timeout or transport failure. A denied
evaluator returns the existing failure path without calling the adapter.

- [ ] **Step 5: Gate the local task verifier**

In the Orchestrator, admit a `verifier_effect` before `run_verifier`, mark dispatched, then dispatch.
Treat a verifier exception as unknown only when the subprocess may have started; otherwise release.
A verifier denial produces the existing unavailable/error path without creating verifier logs.

- [ ] **Step 6: Run targeted tests**

Run: `.venv/bin/python -m pytest tests/test_benchmark_streaming.py tests/test_verifier.py tests/test_orchestrator.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harness/orchestrator.py src/harness/benchmark/runner.py \
  tests/test_benchmark_streaming.py tests/test_verifier.py tests/test_orchestrator.py
git commit -m "feat(benchmark): admit submissions and evaluation before dispatch"
```

## Task 5: Add lease-effect identity for Pi tools

**Files:**
- Modify: src/harness/solver_loop.py
- Modify: src/harness/runtime/bootstrap.py
- Modify: src/harness/orchestrator.py
- Modify: src/harness/benchmark/runner.py
- Test: tests/test_solver_loop.py
- Test: tests/test_executors.py

**Interfaces:**
- Consumes: `ActionAuthority.observe_lease_effect` and an active `AttemptLease`.
- Produces: tool.call/tool.result events carrying `action_id` (the correlated tool call ID),
  `attempt_id`, `lease_epoch` and a stable causal relationship.
- Produces: a stale or ambiguous lease result in an `action.lease_conflict` trace event, no progress
  mutation and no budget charge.

- [ ] **Step 1: Write failing lease attribution tests**

Add to tests/test_solver_loop.py:

```python
def test_pi_tool_call_receives_attempt_and_lease_identity(tmp_path) -> None:
    authority = _authority_with_lease(tmp_path, "attempt:primary", epoch=1)
    loop = _loop(tmp_path, action_authority=authority, attempt_lease=lease)

    asyncio.run(loop.process_event(_session(), AgentEvent(
        type="tool.call",
        data={"tool": "bash", "tool_call_id": "call-1"},
        event_id="evt-call-1",
    )))

    snapshot = authority.snapshot()
    assert snapshot.actions["call-1"] == "observed"
    assert snapshot.action_attempts["call-1"] == "attempt:primary"
    assert snapshot.lease_epochs["attempt:primary"] == 1
    assert [r.kind for r in authority.appender.records()].count("budget.settled") == 1


def test_stale_lease_tool_event_does_not_mutate_progress(tmp_path) -> None:
    old_lease, _ = _issue_two_leases(tmp_path, "attempt:primary")
    loop = _loop(tmp_path, action_authority=authority, attempt_lease=old_lease)
    before = loop.progress.tool_calls

    asyncio.run(loop.process_event(_session(), AgentEvent(
        type="tool.call",
        data={"tool": "bash", "tool_call_id": "stale-call"},
        event_id="evt-stale",
    )))

    assert loop.progress.tool_calls == before
    assert "action.lease_conflict" in _trace_types(tmp_path)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_solver_loop.py -k 'lease or attempt' -q`

Expected: FAIL because SolverLoop has no authority/lease inputs.

- [ ] **Step 3: Pass authority and lease through bootstrap**

Add optional `action_authority` and `attempt_lease` parameters to `bootstrap_solver` and
`SolverLoop`. Existing callers and tests that omit them retain v2 behavior. Compose the active lease in
the local Orchestrator and each benchmark executor.

- [ ] **Step 4: Observe lease effects without claiming pre-dispatch approval**

After legacy correlation and before `progress.record_event`, call
`observe_lease_effect` for `tool.call` and `tool.result` when both authority and lease are present.
For `tool.call`, record the action and charge one tool call. For `tool.result`, record the observed
result without charging again. Include `action_id`, `attempt_id` and `lease_epoch` in the trace data
emitted by `SolverLoop`.

Do not emit `action.admitted` for a lease effect. The event kind and documentation must make clear
that the lease authorized the container operation and the Harness learned the command identity only
after observation.

- [ ] **Step 5: Handle lease conflicts conservatively**

If the lease is stale or the action ID is already bound to another attempt, emit
`action.lease_conflict`, skip the action/progress mutation and do not count it as a new tool call.
Do not silently attribute it to the current session.

- [ ] **Step 6: Run the targeted tests**

Run: `.venv/bin/python -m pytest tests/test_solver_loop.py tests/test_executors.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/harness/solver_loop.py src/harness/runtime/bootstrap.py \
  src/harness/orchestrator.py src/harness/benchmark/runner.py \
  tests/test_solver_loop.py tests/test_executors.py
git commit -m "feat(runtime): correlate Pi tool effects with attempts"
```

## Task 6: Make unknown reconciliation durable and crash-safe

**Files:**
- Modify: src/harness/runtime/checkpoint.py
- Modify: src/harness/orchestrator.py
- Test: tests/test_authority_recovery.py
- Test: tests/test_checkpoint.py

**Interfaces:**
- Consumes: `ActionAuthority`, `reconcile_pending_actions`, `LedgerAppender.records`.
- Produces: `ActionAuthority.from_records(appender, run_id, mode, budget_limits) -> ActionAuthority`
  that replays existing authority events.
- Produces: reconciliation that appends ledger events first and only then updates v2
  progress/checkpoint artifacts.
- Produces: terminal-run reconciliation that appends a new decision without reopening or rewriting
  the terminal result. `action.reconciliation.json` remains a derived report.

- [ ] **Step 1: Write the failing reopen/idempotency tests**

Create tests/test_authority_recovery.py with a parameterized five-window test:

```python
@pytest.mark.parametrize(
    ("window", "expected_state", "held_tool_calls"),
    [
        ("before_admit", "absent", 0),
        ("after_admit_before_dispatch", "reserved", 1),
        ("after_dispatch_before_response", "dispatched", 1),
        ("after_response_before_settle", "dispatched", 1),
        ("after_settle_before_projection", "settled", 0),
    ],
)
def test_authority_replays_crash_windows_without_double_charge(
    tmp_path, window, expected_state, held_tool_calls
) -> None:
    appender = SQLiteLedgerAppender(tmp_path / "ledger.db")
    authority = _authority(appender)
    admission = _drive_to_window(authority, window)

    reopened = ActionAuthority.from_records(
        appender=appender,
        run_id=authority.run_id,
        mode="authoritative",
        budget_limits=_limits(),
    )
    snapshot = reopened.snapshot()

    assert snapshot.actions.get(admission.action_id, "absent") == expected_state
    assert snapshot.held.tool_calls == held_tool_calls
```

Add a test that settling after reopen is idempotent, and a test that an unknown action cannot be
released and can only be reconciled by an explicit outcome.

- [ ] **Step 2: Run the recovery tests and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_authority_recovery.py -q`

Expected: FAIL until replay and reconciliation are implemented.

- [ ] **Step 3: Implement durable replay**

Build authority state only from versioned authority payloads in `LedgerRecord.payload`, with the
top-level `run_id`, `attempt_id`, `correlation_id` and `producer` used for validation. Reject
malformed or cross-Run authority events instead of guessing. Rebuild lease epochs, reservations,
action states and settled/held usage before any new append.

- [ ] **Step 4: Append reconciliation before v2 mutation**

Change `reconcile_pending_actions` so a live run does the following under the existing cross-process
lock:

1. Load and validate the ledger-backed authority.
2. Append the reconciliation and any settlement/release in one authoritative batch.
3. Re-run the ledger verification.
4. Update `progress.json`, `checkpoint.json` and `action.reconciliation.json` only after the append
   succeeds.

If the v2 write fails after the ledger append, a retry with the same outcomes returns the existing
ledger decision and then retries the projection write. It must not append a second settlement.

- [ ] **Step 5: Preserve terminal history**

For a terminal Run with unresolved unknown actions, allow an operator reconciliation that appends
`action.reconciled` and writes a new derived report, but does not modify `result.json`,
`checkpoint.json` or the original terminal status. The terminal result may carry unresolved unknown
IDs, and the objective status must not become `verified` from a reconciliation alone.

- [ ] **Step 6: Add crash-point evidence**

Use a deterministic fault-injection wrapper around the five windows to prove the ledger survives
process restart. The tests must not depend on wall-clock sleeps. Record the expected state and held
budget in assertions. The Docker-backed matrix remains a release gate and must be run explicitly
when the environment is available; do not treat a local skip as evidence.

- [ ] **Step 7: Run checkpoint and recovery tests**

Run: `.venv/bin/python -m pytest tests/test_authority_recovery.py tests/test_checkpoint.py tests/test_orchestrator.py -q`

Expected: PASS for the offline matrix; report environment-dependent skips separately.

- [ ] **Step 8: Commit**

```bash
git add src/harness/runtime/checkpoint.py src/harness/orchestrator.py \
  tests/test_authority_recovery.py tests/test_checkpoint.py
git commit -m "feat(runtime): reconcile unknown actions durably"
```

## Task 7: Freeze mode, update boundaries, and verify the whole branch

**Files:**
- Modify: src/harness/kernel/__init__.py
- Modify: src/harness/gateway_runtime.py
- Modify: src/harness/cli.py
- Modify: tests/test_architecture.py
- Modify: tests/test_control_metrics.py
- Create: docs/adr/0003-action-admission-and-reservation.md
- Modify: docs/architecture.md
- Modify: docs/roadmap/runtime.md
- Modify: docs/roadmap/quality.md

**Interfaces:**
- Consumes: the completed authority paths from Tasks 1-6.
- Produces: `authority_mode` on the composition roots with default `shadow`; the CLI flag
  `--authority-mode` accepts `shadow` or `authoritative`.
- Produces: `validate_authority_startup(*, mode, has_appender, has_run_bound_gateway) -> None`, which
  raises `AuthorityUnavailable` when authoritative mode lacks either requirement.
- Produces: `harness.kernel.action` registered in `CONTROL_SPINE`, with measured module and line
  ceilings.
- Produces: ADR and roadmap evidence that distinguishes implemented Stage 2 shadows from the later
  Stage 4 production read-path switch.

- [ ] **Step 1: Add failing mode/architecture checks**

Add tests that:

```python
def test_authoritative_mode_without_ledger_fails_before_gateway_start() -> None:
    with pytest.raises(AuthorityUnavailable, match="ledger"):
        validate_authority_startup(
            mode="authoritative", has_appender=False, has_run_bound_gateway=True
        )


def test_authoritative_mode_without_run_bound_gateway_fails_closed() -> None:
    with pytest.raises(AuthorityUnavailable, match="gateway"):
        validate_authority_startup(
            mode="authoritative", has_appender=True, has_run_bound_gateway=False
        )


def test_kernel_action_is_in_the_control_spine() -> None:
    assert "harness.kernel.action" in CONTROL_SPINE
```

Also add a docs-consistency assertion for the new ADR link and the documented authority mode.

- [ ] **Step 2: Run them and confirm RED**

Run: `.venv/bin/python -m pytest tests/test_architecture.py tests/test_control_metrics.py tests/test_docs_consistency.py -q`

Expected: FAIL until registration and documentation land.

- [ ] **Step 3: Implement mode selection and rollback**

Add `authority_mode: Literal["shadow", "authoritative"] = "shadow"` to the relevant composition
objects and the CLI flag. Do not introduce a second environment-variable configuration path in this
stage. In authoritative mode:

- require a run-bound Gateway before Agent startup;
- require the shared ledger appender;
- refuse a benchmark Run whose model traffic uses a direct provider rather than a run-bound Gateway;
- fail startup if the appender cannot be constructed or verified;
- leave v2 result schemas and read paths unchanged.

In shadow mode, a missing appender is an explicit degradation, not a failed Run. Document that
authoritative mode is not enabled by default and that Stage 4 has not started.

- [ ] **Step 4: Update architecture and control metrics**

Add `harness.kernel.action` to `CONTROL_SPINE` deliberately and update the architecture guard if the
new module imports only kernel/ledger/pydantic primitives. Re-measure modules and source lines;
update `MAX_MODULES` and `MAX_SRC_LINES` to the measured values, not estimates. The expected
module delta is one; if it grows, justify each additional module in the ADR.

- [ ] **Step 5: Write the ADR and roadmap evidence**

The ADR must state:

- the five crash windows and the unknown-no-retry rule;
- single-writer and atomic admission/reservation semantics;
- shadow versus authoritative behavior;
- the lease-effect exception and its soft overshoot;
- the legacy standalone Gateway/direct-provider boundary;
- what Stage 2 does not claim about exactly-once or production authority.

Update runtime/quality roadmaps only after the evidence exists. Do not mark Stage 4 or a production
ledger read path complete.

- [ ] **Step 6: Run the complete offline verification matrix**

Run:

```bash
.venv/bin/python -m pytest tests/test_kernel_action.py tests/test_gateway_admission.py \
  tests/test_authority_recovery.py -q
.venv/bin/python -m pytest tests/test_architecture.py -q
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_docs_consistency.py -q
```

Expected: targeted tests PASS, architecture PASS, Ruff PASS, full suite PASS except documented
environment-dependent skips, docs consistency PASS. Do not claim Docker/TSeS coverage from this
offline run.

- [ ] **Step 7: Commit and deliver**

```bash
git add src/harness/kernel src/harness/gateway.py src/harness/gateway_runtime.py \
  src/harness/orchestrator.py src/harness/benchmark/runner.py src/harness/solver_loop.py \
  src/harness/runtime/bootstrap.py src/harness/runtime/checkpoint.py src/harness/cli.py \
  src/harness/policy.py src/harness/trace.py tests docs
git commit -m "feat(kernel): enforce stage-two action admission"
git push -u origin <feature-branch>
```

Then follow the repository delivery rule: merge the feature branch into the latest main when the
verification gates pass and push main. Never force-push. Report the actual local and remote SHAs,
plus the environment-dependent exclusions.

## Self-Review Checklist

- [ ] Every Stage 2 requirement has an owning task: model admission, submission admission, verifier
      admission, action/reservation events, release/settle, unknown reconciliation and lease-effect
      identity.
- [ ] Every authority state transition has a positive and a negative test.
- [ ] A denied action has a test proving zero upstream or adapter calls.
- [ ] A ledger failure has a test proving no dangerous dispatch in authoritative mode.
- [ ] A stale lease has a test proving no mutation or charge in another Attempt.
- [ ] A duplicate settle has a test proving one charge and no extra ledger event.
- [ ] A lost response has a test proving the action stays unknown and is not retried.
- [ ] The plan does not claim v3 is the production read path or that shadow mode is strict.
- [ ] New module/line ceilings are re-measured rather than estimated.
