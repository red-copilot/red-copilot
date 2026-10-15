# report.submit

domain: report  
tags: report, submit, vrp

Format and submit vulnerability report to ZC platform with PoC evidence.

## 何时使用

- `observation:vuln.confirmed`（信任级别 ≥ claim）
- `artifact:vuln.poc`（信任级别 ≥ claim）

## 预期结论

- `observation:report.submitted`

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
