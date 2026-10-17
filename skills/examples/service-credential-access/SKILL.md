# service.credential-access

domain: network  
tags: access, credentials, service

Use a discovered service with known or default credentials to obtain access.

## 何时使用

- `entity:network.service`（信任级别 ≥ claim）
- `capability:network.reachability`（信任级别 ≥ evidence）

## 预期结论

- `capability:service.access`
- `artifact:credential`

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后写入 ``；竞争会话可把摘要同步到 ``，不提交 typed World 事件。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
