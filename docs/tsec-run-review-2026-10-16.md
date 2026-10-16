# TSec 全量 sweep 复盘（2026-10-16）

> 依据：`.harness/tsec-runs/` 下的 36 份 `result.json`、`.harness/logs/sweep.log`（30 次重试）、
> `harness report` / `harness audit-run` 的审计输出，以及失败 run 里的
> `agent-workspace/agent.summary.md` 与 `progress.json`。
> 定位：只做复盘与优化项定义，不改执行边界；平台侧动作在 §6 列成 operator 手册，本轮不代执行。

## 1. 数据来源与复算方法

| 口径 | 命令 |
| --- | --- |
| 单题结果 | `harness report .harness/tsec-runs/<run_id>` |
| 证据链审计 | `harness audit-run .harness/tsec-runs/<run_id>` |
| 结果聚合 | `jq -s '…' .harness/tsec-runs/*/result.json`（见下方统计） |
| 空目录清理 | `harness gc-runs --root .harness/tsec-runs`（dry run，2026-10-16 记 43 个） |

全量题集 42 题：`a-01..a-18`、`b-01..b-03`、`c-01..c-07`、`e1-01..e1-06`、`e2-01..e2-04`、
`e3-01..e3-04`。本轮实际产出 36 份结果，缺 6 题：`b-03`、`c-02`、`c-03`、`c-04`、`c-05`、`c-06`。

## 2. 结果面

- **通过率**：36 战 21 胜 15 负（58.3%）；分系列看，`e1/e2/e3` 系列 11/12，`a/b/c` 系列 10/24。
- **失败分类**：15 个失败里 11 个 `finished`（Agent 自己收尾，耗时 1070–1817s，几乎贴着 1800s 墙钟）、
  3 个 `timeout`、1 个 `budget_exceeded`（后四者耗时 1980–2000s，即 1800s 预算 + 180s 宽限用尽）。
- **失败全部零提交**：15 个失败的 `submissions` 都是 `[]`，21 个成功 run 各写了一份
  `agent-workspace/submission.inbox.jsonl`。也就是说失败不是"猜错 flag"，而是"根本没提交过"。
- **成本极度不对称**：失败烧掉 13,855,420 累计 token / 25,430s 墙钟 / 559 次模型调用；
  成功只用 5,647,042 token / 11,388s / 386 次调用。失败占总 token 的 71%。
- **成功很快**：成功 run 墙钟中位数 420s，最快 `e1-02` 24s（3 次模型调用）。
- **失败很慢**：失败 run 墙钟中位数 1751s（15 个里 10 个 ≥ 1721s），`c-01` 用了 70 次模型调用仍为 0 分。

结论：当前瓶颈不是"差一点猜中"，而是**一整轮预算被消耗在从未提交候选的执行上**。

## 3. 过程面

### 3.1 sweep 时间线

| 时间（CST） | 事件 |
| --- | --- |
| 15:03–15:14 | 早前的 `c-*` 小范围尝试：`c-07` 成功，`c-03`/`c-06` 起了 run 但没结果 |
| 15:15:46–19:12 | 主 sweep：产出本轮绝大多数结果，结尾在 3 个并发 worker 上撞到 `Temporary failure in name resolution`（`tsecbench.zc.tencent.com`）而**整轮中止**，`b-03` 等正在跑的题被连带取消 |
| 19:12–19:26 | 外层 bash 重试循环又跑了 28 次：`10.0.100.58` 健康检查超时（`TSec VPN preflight failed`）、DNS 失败、以及 `InvalidState: max active challenge instances reached (3)`（平台仍挂着 3 个未关闭实例）交替出现，无一次成功 |
| 19:26 | 重试循环跑满 30 次自行退出；`.harness/logs/sweep.log` 保留为证据 |

### 3.2 编排层面观察到的四个问题

1. **单个 case 的 provision 失败会中止整轮 sweep**。旧实现把每个 case 的 `run_case` 交给
   `asyncio.gather`，第一个异常直接向上抛并取消同批其它 case，所以 19:12 的 DNS 失败让正在跑的
   `b-03` 等题白跑（20 个目录只有 `trace.jsonl`/`world.*`，没有 `result.json`）。
2. **run 目录先于 provision 创建**。`.harness/tsec-runs/` 里 99 个目录只有 36 个有结果：
   63 个没有 `result.json`，其中 43 个完全没有工件，20 个只有部分工件（`trace.jsonl`、
   `world.*` 等，多为中途被取消的 run）。
3. **平台不可用时没有闸门**。VPN 健康检查失败、DNS 失败、实例占满这三种平台级状态只在第一个
   challenge 启动时才暴露，重试策略是"整轮重来 + 20s 固定间隔"，于是 28 次失败白烧时间，
   并在 runs root 累积出 63 个无结果目录（43 空 + 20 部分）。
4. **断点续跑缺失**。`--all` 只按平台 `is_completed` 过滤，本地已成功的结果不会被跳过；
   `a-03`/`a-05`/`e2-04`/`e3-04` 四题各留下 15 个目录。`--seed-notes-from` 通道存在，但 15 个失败里只有 3 个真写出了
   `agent.summary.md`，而且它默认读 `<run>/agent.summary.md`，与 benchmark 实际的
   `<run>/agent-workspace/agent.summary.md` 不一致，等于没用上。

### 3.3 运行内观察到的四个问题

1. **finalize 只要报告、不要提交**。soft time limit 注入的指令是"停止探测、写四段报告"，没有
   "先把手上最佳候选 flag 提交掉"。15/15 失败零提交与此直接相关。
2. **验证通道在 benchmark 里基本是噪声**。`a-05` 一次 run 收到 99 次 `world.state.updated` +
   39 次 `solver.verification`，其中 verdict 全是 `pending`、`replan_required=true`、
   理由 `world_revision_changed` / `expected_observation_missing`；`a-03` 是
   verified 0 / execution_succeeded 40 / pending 37。对没有本地动作级验证器的 benchmark 来说，
   这些 steer 既消耗上下文又诱导无谓的 replan。
3. **summary 缺失导致续跑无料可用**。`_maybe_reask_finalize` 只在 `pi.agent_settled` 后补一次，
   预算宽限期内被 stop 掉的 run 就什么都不留。
4. **工具能力与配置不一致**。`a-17`、`b-01` 的失败报告都写着 `nmap` 被沙箱拒绝
   （"Operation not permitted"），而 agent yaml 声明了 `cap_add: NET_RAW`；两份报告都在
   "有实质进展但没找到 flag"处结束，侦察被拖慢是可见的。

## 4. 优化清单（四簇）

| # | 簇 | 证据 | 改法 | 预期收益 | 验收指标 |
| --- | --- | --- | --- | --- | --- |
| 1 | 编排 | 28 次失败重试、3 个泄漏实例 | sweep 前置有界在线预检（VPN + 平台列表），失败即分类退出 | 平台不可用时 5s 内退出，不再整轮重来 | 无 run 目录产生；退出码 3；耗时 < 60s |
| 2 | 编排 | 63 个无结果目录（43 空 + 20 部分） | provision 成功后才建 run 目录；`harness gc-runs` 清扫 | 失败不再污染 runs root | 空目录计数归零 |
| 3 | 编排 | 19:12 DNS 中止整轮 | 单题失败只记该题；平台级失败停止派发剩余题 | 一题失败不再毁掉同批结果 | 单题失败时其余题仍产出结果 |
| 4 | 编排 | 同一题 15 个目录 | 本地已成功的题默认跳过；同题最近一次 `agent.summary.md` 自动续接 | 重跑只花在未解题上 | `skipped` 列表出现在 sweep 汇总 |
| 5 | 编排 | 只能靠 jq 手工统计 | 每轮写 `sweep-summary-<UTC>.json`；退出码 0/3/4 区分 | 结果面变成一件文件 | 汇总字段与本文 §2 对齐 |
| 6 | 运行内 | 15/15 失败零提交 | finalize 指令先要求提交最佳候选，再写报告 | 把"没提交"转成"至少提交" | 限时 run 的 finalize 文本含提交通道 |
| 7 | 运行内 | 失败 run 半程后仍零提交 | 墙钟过半且零提交时注入一次 `solver.submission_due` | 中途纠偏，而非撞线 | 每 run 至多一次；提交后不再触发 |
| 8 | 运行内 | a-05 99+39 次 steer | benchmark 模式不再回灌 Agent 自写的世界变更，`pending` verdict 不再 steer | 单 run steer 从上百降到个位数 | 自写变更不产生 steer；pending 只进 trace |
| 9 | 运行内 | 3/15 写出 summary | 同题最近 summary 自动续接 + 路径兼容 workspace 布局 | 续跑有料可用 | `--seed-notes-from` 指向 run 目录即可 |
| 10 | 可观测 | accepted/rejected 无法区分"没试过" | run metrics 与 ledger 增加 `submissions_attempted`、`first_submission_ms` | 失败可分类归因 | 汇总与 run 结果都带该字段 |
| 11 | 工具 | a-17/b-01 报告 nmap EPERM | 核对 `docker/pi-kali` 与 `pi_adapter` 的 cap/seccomp，或让 prompt 改走 TCP connect 扫描 | 恢复端口侦察 | 见 §7（本轮未改执行边界） |

## 5. 本轮已落地的改动

- **BEN-05 编排可靠性**（`benchmark/tsec_client.py`、`benchmark/tsec_adapter.py`、
  `benchmark/runner.py`、`runtime/results.py`、`cli.py`）：平台失败分类
  （`vpn_unreachable` / `platform_unreachable` / `instance_conflict`）、有界预检门、
  单题失败隔离 + 平台级失败停止派发、provision 成功后才建 run 目录、断点续跑与自动续接、
  `sweep-summary-<UTC>.json`、退出码 0/3/4、`harness gc-runs`。
- **RUN-05 运行内收敛**（`solver_loop.py`、`runtime/bootstrap.py`、`benchmark/runner.py`、
  `progress.py`）：finalize 先提交最佳候选再写报告、半程零提交 steer、benchmark 模式 steer 降噪、
  `submissions_attempted` / `first_submission_ms` 指标。
- **实测证据**：接上真实平台（当前健康检查仍失败）跑 `harness tsec --all --runs-root /tmp/runs-probe`
  → 退出码 3、耗时 5.0s、run 目录 0 个、汇总写入 `platform_error.kind=vpn_unreachable`；
  `harness gc-runs` 在真实目录上 dry run 报出 43 个空目录。

## 6. 平台侧恢复步骤（operator 手册，本轮未执行）

1. 关闭平台上挂着的 3 个 challenge 实例（`InvalidState: max active challenge instances reached (3)`），
   否则任何新 sweep 都会在 start 阶段 409。
2. 确认隧道与平台：`http://10.0.100.58` 健康检查可达、`tsecbench.zc.tencent.com` 能解析
   （本轮观测到健康检查超时与 DNS 失败交替出现）。
3. 用修复后的单次 sweep 替代 bash 重试循环：
   `harness tsec --agent agents/examples/pi-tsec-opencode.yaml --all`，失败时按退出码处理——
   3 表示平台不可用（修平台，不要盲目重试），4 表示部分题没产出（看 `sweep-summary-*.json`）。
4. 重跑目标：6 个缺题（`b-03`、`c-02`..`c-06`）+ 4 个未解题（`a-03`、`a-05`、`e2-04`、`e3-04`）。
   其余题因本地已有 `objective_completed` 结果会被 `--resume` 自动跳过，需要重跑时加 `--rerun`。
5. 清理垃圾目录（可选，干掉 43 个空目录）：`harness gc-runs --root .harness/tsec-runs --apply`。

## 7. 未完成与风险

- **工具能力（第 11 项）未改**：nmap 的 cap/seccomp 结论需要动执行边界，按仓库约定另开 PR 并走
  maintainer 复核；本轮只在报告里给出证据与两条备选路线（补 raw socket 能力，或让 prompt 改走
  TCP connect 扫描）。
- **真实验收未做**：平台当前不可用，§4 中与通过率相关的验收（重跑 10 题）需要在平台恢复后补测，
  本轮只能给出本地可复现的证据（退出码、目录数、steer 行为、指标字段）。
- **历史目录保留**：63 个无结果目录（43 空 + 20 部分）已清点但未删除，等待 operator 显式执行
  `gc-runs --apply`。
