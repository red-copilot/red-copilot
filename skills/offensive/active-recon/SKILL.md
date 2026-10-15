# recon.active

domain: recon  
tags: recon, active, nmap, portscan

Active port scanning and service version detection on authorized targets.

## 何时使用

- `capability:network.reachability`（信任级别 ≥ evidence）

## 预期结论

- `observation:network.service`
- `entity:network.service`

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后追加到 `$HARNESS_EVENT_FILE`（`world.observe` / `world.hypothesis` / `world.artifact`）。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
