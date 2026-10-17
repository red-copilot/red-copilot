# benchmark.flag-candidate-verification

domain: benchmark  
tags: flag, submission, verification

Confirm a candidate flag by submitting it to the platform and reading authoritative feedback.

## 何时使用

- `artifact:flag.candidate`（信任级别 ≥ claim）

## 预期结论

- `observation:flag.verified`（信任级别 ≥ verified）

## 提示

- 用授权范围内的最小化动作推进，不做破坏性利用或持久化。
- 结论写完后写入 ``；竞争会话可把摘要同步到 ``，不提交 typed World 事件。
- 关键发现与排除路径记到 `$HARNESS_NOTES_DIR` 下的 markdown 笔记。
