# PLN — Planning, skills and agent cognition

**Scope:** `solver_loop.py`, `solver_policy.py`, `progress.py`, `extract.py`, `skills/`.

The v1 planning workstream was retired by [agent-architecture-v2.md](../agent-architecture-v2.md):
Harness no longer plans, ranks skills or compiles rounds. Cognition belongs to the Agent, which
keeps its own markdown notes and reads read-only knowledge roots on demand.

### PLN-01 [!] — Replan on actual information change
Descoped: there is no planner to replan. The loop keeps only the external budget/wall-clock stop
law (`solver_policy.decide_solver_action`); repeated failures and identical commands do not stop
or steer the Agent. The Agent redirects itself.

### PLN-02 [!] — Planner ablations and safe fallback
Descoped with `--solver-profile`. The v1 frontier planner survives as an offline baseline
(`tests/ablation/`, `tests/test_ablation_frontier.py`) so the ablation stays reproducible; it is
not reachable from the package or the control spine.

### PLN-03 [!] — Skill interfaces
Descoped: `SkillSpec`, `StateSelector`, `harness validate-skill` and the YAML catalogue are gone.
Skills are `skills/**/<skill-id>/SKILL.md` - plain language the Agent reads with its own tools,
with no executable semantics and nothing parsing them.

### PLN-04 [x] P1 — Competing solver sessions
Deliver: `AgentSpec.executors` runs several solver sessions against one provisioned challenge
(different provider/model/thinking/args), sharing submission dedupe; the first accepted
submission closes the rest. Instances stay within the platform's three-per-team cap.
Accept: an offline fixture proves N sessions start on one provision, a duplicate candidate is
submitted once, and the first success cancels the others.
State: implemented. `runtime/executors.py` plans one isolated session per executor
(`run_dir/executors/<nn>-<id>/`, own workspace, notes, progress ledger and checkpoint, at most
four), splits the token/model-call/tool-call/cost budgets evenly while keeping the wall-clock
window whole, and exposes the single cross-session writable channel
`run_dir/executor-shared/` (findings plus Harness archive snapshots).
`BenchmarkRunner.run_case` provisions once, races the sessions over one World, one trace, one
submission dedupe set and one evaluator verdict, cancels the losers on the first confirmed
success, and reports every session under `result.json:executor_sessions[]`. The local
`Orchestrator._run` path still fails closed on a multi-executor declaration.
Evidence: `tests/test_executors.py` (shared provision, submit-once dedupe, first-success
cancellation, per-session layout, budget shares, session cap, shared-findings boundary) and
`tests/test_architecture.py` / `tests/test_control_metrics.py` for the module and ceiling
updates.

### PLN-05 [!] — Goal-driven intent-fact frontier
Descoped: `planner.py`, `cognition.py`, `Goal.required_states` and `ObjectiveSpec.required_states`
were removed. The implementation is frozen under `tests/ablation/` for ablation only.

### PLN-06 [!] — Round-based execution over the fact/intent graph
Descoped: there is no graph to derive rounds from. The Agent owns its own notes
(`$HARNESS_NOTES_DIR`), which is what the compiled round brief was approximating; `extract.py`
still derives evidence-tier facts from tool output for the world projection.

### PLN-07 [!] — Agent-visible skill catalog
Descoped: the catalog is no longer a Harness projection. `skills/` is mounted read-only under
`/knowledge/<name>` and discovered by the Agent with `ls` / `cat`.

### PLN-08 [x] P2 — Tool and memory discipline (markdown only)
Deliver: `prompts/tsec-solver.md` says what the tool budget is per call and when to write
conclusions back into notes; `skills/offensive/*` carries ffuf rate/recursion guidance and the
fallback path when a scanner is unavailable; `knowledge/methodology/*` says when to abandon a
path. Nothing is parsed, ranked or injected by the Harness.
Accept: the knowledge catalogue and docs consistency tests pass; the next live sweep reports
slow-call counts and notes bytes as observations, not gates.
State: implemented. Evidence: the prompt asked for ffuf/gobuster while the Agent hand-rolled 287
`echo`, 191 `cat` and 99 `for` invocations; notes stayed at a 344-byte median and a-16 ended with
"Findings: (none yet)" after 2000s. The prompt now bounds a single call (a self-imposed timeout,
and 180s is the point where the Harness records a slow call), requires a notes write-back every
5-10 tool calls or on every settled conclusion, and states that a candidate is submitted the
moment it exists rather than at the finalize window; `skills/offensive/web-crawl` carries the
ffuf rate/recursion starting point and the scanner-unavailable fallback, `vuln-scan` separates
"the scanner failed" from "the target is clean", and `knowledge/methodology/recon-to-report.md`
names the minutes-long-call and the silent-notes triggers for abandoning a path. Nothing is
parsed, ranked or injected by the Harness; `tests/test_knowledge_catalogue.py`,
`tests/test_pi_kali_image.py` (no solve-time install in agent-facing markdown) and
`tests/test_docs_consistency.py` stay green. Slow-call counts and notes bytes are observations
for the next sweep, not gates.

**Non-goals:** required task DAG, harness-authored chain-of-thought, a learnable planner as the
main spine, harness-computed skill ranking.
