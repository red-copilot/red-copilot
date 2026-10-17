# PLN — Planning, skills and agent cognition

**Scope:** `solver_loop.py`, `solver_policy.py`, `progress.py`, `extract.py`, `skills/`.

The v1 planning workstream was retired by [agent-architecture-v2.md](../agent-architecture-v2.md):
Harness no longer plans, ranks skills or compiles rounds. Cognition belongs to the Agent, which
keeps its own markdown notes and reads read-only knowledge roots on demand.

### PLN-01 [!] — Replan on actual information change
Descoped: there is no planner to replan. The loop keeps a pure stop law
(`solver_policy.decide_solver_action`: no-progress, unconfirmed-progress, budget, low-budget) and
the Agent redirects itself. Budget thresholds still steer convergence once each.

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

**Non-goals:** required task DAG, harness-authored chain-of-thought, a learnable planner as the
main spine, harness-computed skill ranking.
