# network.service-discovery

domain: network  
tags: discovery, network

Discover reachable network services and record observations.

## 何时使用

- `capability:network.reachability`

## 预期结论

- `observation:network.service`
- `entity:network.service`

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后写入 ``；竞争会话可把摘要同步到 ``，不提交 typed World 事件。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
