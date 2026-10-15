# PLN — Planning, skills and coordination

**Scope:** `planner.py`, `skills.py`, `progress.py`, `coordination.py`, `solver_loop.py`. Rolling horizon 1–3 and heuristic skill ranking already exist.

### PLN-01 [x] P1 — Replan on actual information change
Deliver: decisions reference goal, world revision, verified/claimed evidence, failures and remaining budget. Distinguish real new evidence from repeated text/tool success. Rate-limit repeated plans and avoid stale skill prerequisites.
Accept: deterministic fixtures show verification failure, contradiction and capability gain yield changed decisions; no endless identical replans.

Implemented in `RollingHorizonPlanner` and `SolverLoop`: ranking considers current goal tokens, world records/provenance, skill failures, and remaining budget; expired or insufficiently trusted prerequisites do not satisfy a skill. Replanning follows world/evidence changes and uses a state signature to suppress identical repeats. Refuted hypotheses now set an explicit replan reason as well as annotating action triggers. Deterministic tests cover verification failure, refuted hypotheses, capability gain, failed-skill penalty, trust-qualified prerequisites, and repeated identical signals. See [planner tests](../../tests/test_planner.py) and [solver loop tests](../../tests/test_solver_loop.py). Focused planning/solver tests passed (17 tests); full suite passed (205 passed, 1 Docker integration skipped) on 2026-10-09.

Replanning is now keyed on the recommended action set instead of the raw world revision: the revision advances on almost every event (and the heuristic score drifts with the focus set), so the old signature re-planned and re-notified the Agent for facts that changed no action. `maybe_replan` now suppresses any replan whose ranked `skill_id` list, replan reasons, no-progress count and last verification status are unchanged. Related planner change: a skill whose declared `produces` are all already satisfied is dropped from candidates (novelty `0.0`), because re-running it cannot teach the solver anything. Observed on a live RPC TSec run (`c-03`): 2 replans across 23 tool calls, where a comparable earlier run issued 16 replans across 17 calls. Regression coverage in `tests/test_solver_loop.py` (unrelated world revisions and recurring verifier reasons do not re-publish; a changed frontier does) and `tests/test_planner.py` (redundant skill is excluded). Validation on 2026-10-14: full suite passed (489 passed, 3 skipped); Ruff clean.

The solver now steers the Agent as the budget runs out. Remaining budget is reported per dimension (`ProgressLedger.budget_fractions`, with `remaining_budget_fraction` delegating to it so the two can no longer drift), and crossing 50%, 25% or 10% once each emits a `solver.budget` observation telling the Agent to consolidate verified findings and submit the candidates it already holds instead of opening new exploration. `solver.budget` is in the RPC session's `_STEER_TYPES`, so it is injected as a steer rather than only written to the feedback file. Thresholds fire at most once per run and are not persisted across resume. Verified on a live RPC TSec run (`c-08`): exactly three `solver.budget` observations, one per threshold. Regression coverage in `tests/test_solver_loop.py` (each threshold fires once, no repeat, no fire above the highest threshold). Validation on 2026-10-14: full suite passed (496 passed, 3 skipped); Ruff clean.

### PLN-02 [~] P2 — Planner ablations and safe fallback
Deliver: compare Pi-only, Pi+World, Pi+World+heuristic planner on identical tasks/seeds/budgets. Keep planner optional and non-blocking when skills unavailable.
Accept: published per-domain completion/cost comparisons; if planner does not improve results, retain only optional reference implementation.

`SolverLoop` accepts `planner_enabled=False`; replanning requests are recorded as suppressed and the loop continues without writing or publishing a planner action. The solver composition now exposes `--solver-profile pi-only|pi-world|pi-world-heuristic` for both single runs and suites. Profile selection controls World context and heuristic planning, is stored in run and suite manifests, is checked on resume, and is reported by the evaluation comparator while preserving frozen task/seed checks. Bootstrap tests verify all three settings; comparison tests cover profile reporting and rejection of unknown profiles.

On 2026-10-09, ran the fixed-seed 18-attempt, nine-domain synthetic suite under all three profiles with identical demo-agent and budget settings. Each profile produced 18/18 verified successes, 100% score, 216 tokens, 18 tool calls, and zero false-positive verifications. Pairwise comparisons passed with no regressions. This is plumbing and regression evidence only: the deterministic demo agent does not make meaningful model decisions, so these results do not establish a planner benefit or satisfy the required model-backed per-domain completion/cost comparison. PLN-02 remains in progress pending that evidence. Focused validation: 33 bootstrap/comparison/suite/smoke tests passed. Full validation: 350 passed, 3 skipped; Ruff, compileall, architecture test (1 passed), and `git diff --check` passed. Docker-dependent tests were skipped in this environment.

The maintainer authorized the model-backed evaluation on 2026-10-09. Execution was attempted, but this workspace cannot run it: the configured Pi agent requires a Docker image, neither Docker nor the Pi CLI is installed, no supported provider key is present in the environment, and the checked-in virtualenv points to Python 3.10 although the project requires Python 3.11 (the installed Python 3.11 has no project dependencies). No model requests were made and no evaluation results are claimed. Repeat the fixed-seed three-profile suite once a Python 3.11 environment with dependencies, the Pi image/runtime, and provider credentials is available.

### PLN-03 [x] P2 — Skill interfaces
Deliver: declarative preconditions/produces metadata and applicability reporting, validated schemas and explicit selection rationale; no implicit unreviewed shell execution from skill metadata.
Accept: irrelevant/stale prerequisite rejection, deterministic scoring and injection resistance.

Skill and selector schemas reject unknown top-level execution fields; the planner consumes skill metadata only and never executes a command from it. `HeuristicSkillPlanner.explain` reports applicability and reason codes for missing, stale, scope-mismatched and insufficient-trust prerequisites, and the plan API exposes that report beside ranked candidates. Ranked actions include deterministic selection rationale. Tests cover stale/untrusted/missing preconditions, rejection of an unreviewed top-level `command`, inert nested command metadata, deterministic repeated scores, and candidate rationale. Acceptance criteria are met; see [skill tests](../../tests/test_skills.py), [planner tests](../../tests/test_planner.py), and [control-plane tests](../../tests/test_control_plane.py). Validation on 2026-10-09: focused tests passed (26); full suite passed (352 passed, 3 skipped); Ruff, compileall, architecture test (1 passed), and `git diff --check` passed. Docker-dependent tests were skipped locally.

### PLN-04 [ ] experimental — Multi-agent shared work
Use existing leased blackboard only where parallelism is demonstrably beneficial; add ownership, conflict handling and cancellation.
Accept: no duplicate externally visible actions under lease races and positive measured throughput/quality gains.

### PLN-05 [~] P1 — Goal-driven intent-fact frontier
Deliver: wire the previously unwired intent-fact projection into the solver loop as a
benchmark-agnostic, goal-driven frontier. `Goal.required_states` (seeded from
`ObjectiveSpec.required_states`) declares target state selectors; the frontier keeps only
enabled intents that directly hit an unmet target or are backward-chained prerequisites
(depth-bounded, cycle-safe). The heuristic ranker becomes a scoring term and ablation
baseline.
Accept: deterministic fixtures cover goal-target trust levels, backward chaining, depth
limit, cycle termination, deterministic ordering, and the all-enabled fallback for tasks
with no declared targets; the frontier projection is appended to Agent context within a
character budget; the control-spine closure and size ceilings still hold.

Implemented in `cognition.py` (renamed from `offensive.py`; phase/weight semantics
removed) and wired through `bootstrap_solver`/`SolverLoop`. New `pi-world-goal` profile is
the default; `pi-world-heuristic` is retained as the non-frontier ablation baseline.
`StateSelector` moved to the world layer with `skills.py` re-exporting it, and
`Goal`/`ObjectiveSpec` gained optional `required_states` (backward-compatible). Validation
on 2026-10-14: full suite passed (481 passed, 3 skipped); Ruff clean; architecture and
size-ceiling tests pass (`harness.cognition` added to the frozen control spine; 60
modules / 14,845 source lines). See
[cognition tests](../../tests/test_cognition.py) and
[docs/agent-engineering.md](../agent-engineering.md).

The fixed-seed `pi-world-heuristic` vs `pi-world-goal` per-domain completion/cost ablation
still requires Docker, Pi and provider credentials, none of which are available in this
workspace; it remains pending and no planner-benefit claim is made.

### PLN-06 [~] P1 — Round-based execution over the fact/intent graph
Deliver: a run stops being one long Agent session. When the fact/intent graph changes (a new
evidence/verified fact, or a newly ruled-out path) or the Agent stalls, Harness opens a new round:
a fresh `prompt` on the same session carrying a compiled brief of what the previous round
established, what it ruled out, and the open frontier.
Accept: rounds fire only on real graph change or stall; the brief is bounded and carries facts,
ruled-out paths and the frontier; the Agent is told not to repeat ruled-out paths.

Implemented in `cognition.round_brief` plus a round controller in `SolverLoop`: `ruled_out_intents`
links failed actions, failure records and per-skill failure counts to skill ids, so "tried and
failed" is graph-visible (`CONTRADICTS` only linked fact to fact). Rounds are debounced - an
unchanged brief is not re-sent - and stall rounds (`ROUND_STALL_STEPS = 6` tool results with no new
fact) always fire as the intervention. Harness bookkeeping is excluded from the fact set, because
every tool result also writes a `tool.execution` observation at evidence tier and would otherwise
look like progress on every step. Lock-in that produces no hard error is caught by a new
"repeated without result" signal: `tool.call` now carries a bounded `args_family`
(`bash:for`, `bash:curl`, ...) alongside an exact argument fingerprint, attempts that gained no
fact are counted per family, and families with three or more attempts are listed in the brief.
Verified on live TSec runs (`a-05`, `--wall-time` 150-300): rounds fired, `round-N.md` was written
and delivered as `solver.round`, the brief listed established facts, the open frontier and
`bash:cat × 3` style repetition, and `tool.execution` noise was absent. Regression coverage in
`tests/test_cognition.py` (ruled-out linkage, refuted hypotheses, brief contents and bounds) and
`tests/test_solver_loop.py` (graph-change round, stall round, no duplicate round) plus
`tests/test_pi_adapter.py` (command-family derivation). Validation on 2026-10-14: full suite
passed (509 passed, 3 skipped); Ruff clean. Open: rounds currently fire mostly as `stall` because
the Agent writes few semantic facts; ruled-out-by-evidence stays empty without hard failures.

Follow-ups from the first live rounds: (1) `graph_changed` now also fires on Agent-authored
claims, not only evidence/verified facts, because Agent records are new knowledge the run gained
even while the verdict stays open; the brief lists them in a separate "Leads (agent-claimed,
unverified)" section. (2) A finalize steer injected into a busy turn could be missed entirely -
one live run was killed at the grace deadline with no report - so when a settled Agent has still
not written `agent.summary.md`, `SolverLoop` re-asks once with a fresh `prompt`
(`solver.finalize`, `reason=summary_missing`) rather than relying on the in-turn steer. Verified
on a live `a-05` run: `solver.finalize` fired at the soft deadline and the Agent produced the
report within the grace window (`agent_report.present = true`, 14 summary facts). Regression
coverage in `tests/test_solver_loop.py` (re-ask fires once on settle when the report is missing,
and never when it exists) and `tests/test_cognition.py` (leads section, bookkeeping excluded).
Validation on 2026-10-14: full suite passed (512 passed, 3 skipped); Ruff clean.

The fact surface was the last blocker for `graph_changed` rounds: the graph only grew when the
Agent chose to call `world.observe`, which it rarely did. Harness now derives facts from tool
output itself (`harness/extract.py`): nmap `open` lines become `network.service` observations and
gobuster/dirb/ffuf result lines become `web.endpoint` observations. The adapter keeps a bounded,
sanitized `output_excerpt` plus `exit_code` on `tool.result`; `TypedACI` parses it and upserts
Harness-observed evidence observations with content-addressed ids, so repeated identical output
does not duplicate. Extraction is deliberately conservative: only distinctive regular formats
match, everything is capped at 25 facts per result, and no raw output is added to World. Verified
end-to-end in-process (raw Pi `tool_execution_end` -> normalization -> extraction -> observations)
and on a live run, where 30/30 tool results carried excerpts; that particular run used only
`curl | head`/`cat`, so it correctly produced no facts. Regression coverage in `tests/test_aci.py`
(service/endpoint extraction, evidence provenance, idempotent repeats) and
`tests/test_pi_adapter.py` (excerpt/exit-code normalization feeding extraction). Validation on
2026-10-14: full suite passed (514 passed, 3 skipped); Ruff clean. Open: only the three bundled
recon formats are parsed, so a run that never invokes nmap/gobuster/ffuf still leans on stall
rounds.

The format gap closed on 2026-10-15 on branch `feat/agent-engineering-midterm`: `extract.py` now
also parses the tool the live runs actually used — `curl -v` request/response pairs
(`> GET /path HTTP/1.1` ... `< HTTP/1.1 200 OK` become `web.endpoint` facts; the pairing is what
keeps bare status lines, `curl -i` headers and prose from matching) — plus `whatweb`
(`web.technology` with detected components), `hydra` (`credential.access` with service/host/
login/password), `sqlmap` (`web.vulnerability` with method/parameter/technique for both the
`'...' injectable` and `is vulnerable` forms) and `nikto` (`web.endpoint` with the finding note).
All five stay inside the conservative envelope: distinctive anchors only, the shared 25-facts cap,
content-addressed dedup, evidence-tier observations carrying structured content instead of raw
output. Unit coverage in the new `tests/test_extract.py` (15 cases: one positive and at least one
negative per format, mixed-output regression, cap enforcement); the ACI wiring is unchanged and
still covered by `tests/test_aci.py`. Validation: `pytest tests/test_extract.py tests/test_aci.py
tests/test_pi_adapter.py -q` (53 passed); Ruff clean.

**Non-goals:** required task DAG or harness-authored chain-of-thought.

### PLN-07 [~] P1 — Agent-visible skill catalog
Deliver: deepen the frontier-to-prompt projection so the Agent sees skill metadata, not just
ids: every frontier intent line carries its declared `requires`/`produces` selectors in compact
notation, and a "Next unlocks" section lists pending intents reached by the backward chain with
exactly which unmet selectors keep them disabled. Pi's own skills/extensions stay off; the
catalog is a pure metadata projection.
Accept: deterministic fixtures cover catalog rendering in both `world.context.txt` and round
briefs, the unlock section lists unmet selectors for depth-bounded pending intents only,
character budgets still bound the projection, and no executable content ever enters the
catalog.

Design in [agent-engineering.md §7](../agent-engineering.md).

Implemented on branch `feat/agent-engineering-midterm`: `IntentFactDAG.next_unlocks()`
shares the frontier's backward-chain expansion and collects the pending intents it walks
through as `UnlockHint`s (min depth, merged unmet selectors). `render_frontier` lines now
carry a compact `requires <selectors> -> produces <selectors>` suffix (trust floors shown as
`min_trust=...`), followed by a bounded `### Next unlocks` section listing each pending
intent with exactly the facts that would make it actionable; `round_brief` frontier lines
carry the same suffix. No-target fallback keeps unlocks empty (every enabled intent is
already exposed). Regression coverage in `tests/test_cognition.py::TestAgentVisibleCatalog`
(catalog suffix with trust floor, unlock section with unmet selectors, depth bound and
ordering, empty fallback, deterministic rendering, brief carries selectors). Validation:
`pytest tests/test_cognition.py tests/test_solver_loop.py tests/test_planner.py -q` (69
passed), full suite 547 passed / 3 skipped (Docker-gated), Ruff clean, architecture and
size-ceiling tests pass.
