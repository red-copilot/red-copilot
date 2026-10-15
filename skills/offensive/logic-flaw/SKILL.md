# discovery.logic-flaw

domain: web  
tags: discovery, logic, idor, authz, race

Probe for business logic flaws — IDOR, privilege escalation, race conditions, and auth bypass.

## 何时使用

- `entity:web.endpoint`（信任级别 ≥ claim）
- `observation:web.endpoint`（信任级别 ≥ claim）

## 预期结论

- `hypothesis:logic.vulnerability`
- `observation:web.finding`

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
