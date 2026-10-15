# 智能体工程设计：赛场上的 Agent-Native Harness

> 本文是 v2 设计（[agent-architecture-v2.md](agent-architecture-v2.md)）在赛场方向的落地说明：
> 认知层是什么、会话怎么组、赛场硬约束有哪些、48 小时窗口内先做什么。
> v1 的《智能体工程设计：目标驱动的意图-事实前沿》已随 v2 剪枝删除，其前沿算法冻结为离线消融
> 基线（`tests/ablation/`），不再进入生产路径。

## 1. 定位与归属

一句话：**Harness 供给环境与信任，模型拥有认知。**

| 层 | 归属 | 内容 |
| --- | --- | --- |
| 认知（做什么、怎么做） | 模型 | 推理、选工具、写笔记、读知识 |
| 环境（在哪儿做） | Harness | 容器、只读任务与知识挂载、可写工作区、网络 profile |
| 信任（算不算数） | Harness | 三层信任、授权裁决、证据引用 |
| 记账（花了多少） | Harness | 预算、trace、工件、checkpoint |
| 恢复（崩了怎么办） | Harness | 生命周期、对账、单操作者 flock |

非目标：黑板/蚁群等涌现式协调、可编程 DSL、向量检索、神经世界模型、把模型输出当结论、
Harness 侧规划与技能排序。这些已在 v2 中删除，`tests/test_architecture.py` 会挡住回归。

## 2. 认知层：文件系统 + 通用 coding agent

Agent 的全部"认知基础设施"就是容器里的 markdown：

- **笔记** `$HARNESS_NOTES_DIR`（容器内 `/run/harness/notes`）：Agent 自己写、自己读，
  跨轮次与跨会话的长期记忆。Harness 不解析、不排序、不做 schema 校验。
- **知识** `$HARNESS_KNOWLEDGE_DIRS`：只读 markdown 目录，容器内挂在 `/knowledge/<name>`。
  技能说明 `<skill-id>/SKILL.md`、payload 手册、方法论都走这条路；Agent 用 `ls` / `cat`
  按需取用，Harness 既不注入也不排序。目录本身无可执行语义。
- **世界观测** `$HARNESS_WORLD_CONTEXT`：Harness 投影的**事实**视图——焦点集、已确立事实、
  未解未知、失败记录、预算。它是观测输入，不含候选动作、不含计划。
- **提交** `$HARNESS_SUBMISSION_INBOX`：一行一个 JSON 候选，由 Harness 去重后提交；
  被拒候选是修正假设的证据，不是重发理由。

需要强制执行的东西留在 Harness：授权范围、预算、破坏性动作边界、验证权威。
这条边界让领域知识可以随便换，而不必改运行时代码。

## 3. 执行拓扑：Pi 优先 + 同题多会话竞争

- **执行内核**：`type: pi` 是默认执行器（容器内 Pi RPC）。硬化保持原样：只读根文件系统、
  `--cap-drop ALL`、no-new-privileges、无 docker socket、只读 `/task` 与 `/knowledge/*`、
  可写 `/run/harness`。
- **同题竞争**：`AgentSpec.executors` 声明同一道题上的多个 solver 会话（不同 provider /
  model / thinking / `extra_args` / system prompt）。调度落地在
  `src/harness/runtime/executors.py` 与 `BenchmarkRunner.run_case`：
  - 上位并发：`harness tsec --parallel N` 决定同时跑几道题（平台上限 3 题）。
  - 题内并发：每题按 `executors` 派生 N 个会话，**硬上限 4**（`MAX_EXECUTOR_SESSIONS`，
    超限在加载期与调度期都 fail-closed）。
  - 会话隔离：每个会话独占 `run_dir/executors/<nn>-<id>/`（工作区、笔记、事件、日志、
    会话级 `progress.json` 与 `checkpoint.json`），容器名由该路径派生，互不覆盖。
  - 共享面：一次 provision、一个 world、一条权威 `trace.jsonl`、一个提交去重集合、
    一次最终裁决、一份 `result.json`。`result.json` 的 `executor_sessions[]` 记录每个会话
    的状态、提交次数与预算用量。
  - 共享成果：`run_dir/executor-shared/` 以 rw 挂到各会话的 `/run/harness-shared`，
    只放 Agent 自己写的 `shared-findings.md`（`$HARNESS_SHARED_FINDINGS`）；Harness 按内容
    哈希归档快照。该目录不得出现任何权威工件，见
    [security/execution-boundary.md](security/execution-boundary.md)。
  - 预算：token / 模型调用 / 工具调用 / 成本按会话均分（向下取整、下限 1），
    wall_time 不切分——会话并发共享同一个赛窗。
  - 收敛：任一会话提交被接受且最终 evaluator 判定成功即关闭其余会话并释放实例；
    单个会话触达自身止损律只关自己；全部会话结束或预算耗尽即结束该题。
  - 降级：本地 `harness run`（`Orchestrator._run`）仍对多 executor 声明 fail-closed。

## 4. 赛场约束（闯关赛接入面）

当前客户端保持既有 SDK 接入面：`tsec_benchmark` 的 `openapi/v1/*` + `BENCHMARK_TOKEN`，
控制面凭据只在 Harness 侧，容器里只有目标地址与任务描述。**SDK 与官方文档
（`/api/*` + `Agent-Token`）的差异仍是赛前必须核对的门**，本轮不改接入实现，只登记状态。
SDK 与 MCP 两条面都还没在本机对真实平台跑通，任何"已对齐"的说法都要等 ZCP-01 的证据。

已知的赛场硬约束（来自平台文档，落地状态见 §5）：

- 平台每队同时运行实例**最多 3 个**，用完必须 `close_challenge` 释放，
  否则后续 `start_challenge` 直接失败。
- 全接口共享**每秒 3 次**限流，超限返回 HTTP `429`。
- 查看提示后该题得分扣 10%，且只能查看已启动实例的题；提示只在需要时按阈值取用。
- flag 按题计分，多 flag 题按 flag 计数，重复提交同一 flag 不再得分。
- 机器容量前提：`--parallel` × 每题会话数 = 同时在跑的容器数，
  以 4 GB / 4 CPU 每会话估算本机容量，别把并发堆到同一道题上。

## 5. 48 小时窗口：P0 / P1

P0 是赛前必须成立的能力；没做完的项必须显式标出，不许当成已完成。

| ID | 交付 | 状态 | 验收 |
| --- | --- | --- | --- |
| PLN-04 | 同题多 executor 竞争调度 | 已落地 | `tests/test_executors.py`：一次 provision、重复候选只提交一次、首个成功取消其余会话 |
| ZCP-01 | 接入面核对（SDK `openapi/v1` vs 官方 `/api` + `Agent-Token`） | 未落地 | 对真实平台跑通 list/start/submit/hint/close 并记录端点与认证头 |
| ZCP-02 | 3 req/s 令牌桶 + 实例 ≤3 + 用完 `close_challenge` 释放 | 未落地 | 离线计数测试证明不越界不触发 429；实例数不超上限 |
| ZCP-03 | 多题并行调度（≤3 题，独立 run dir 与预算） | 已落地 | `run_tsec_challenges` 的信号量与按题 run dir（`tests/test_tsec_adapter.py`） |
| ZCP-04 | 按步数阈值分级提示，替代全局 `--hint` | 未落地 | 阈值只触发一次、只影响该题、扣分影响可对账 |

P1 是赛前有余力才做、赛后必做的项：

| ID | 交付 | 状态 | 验收 |
| --- | --- | --- | --- |
| ZCP-05 | 会话级上下文压缩与记忆回注（笔记外置 + 重开会话） | 未落地 | 模拟超长会话后仍保留已确立事实、已排除路径与开放前沿 |
| ZCP-06 | 重复动作打断（连续同命令族 N 次直接注入干预） | 未落地 | 脚本化重复 `curl` 被拦截并解除循环 |
| ZCP-07 | 知识库填充（payload 手册、方法论、writeup 摘要） | 未落地 | `knowledge/` 有可 `ls` / `cat` 的只读内容，且不换取网权限 |

排期建议：Day 1 先做 ZCP-01/02（不核对接入面就没有后续的一切），再做 ZCP-04；
Day 2 视时间做 ZCP-05/06，知识库靠 Agent 现场写笔记兜底。

## 6. 信任边界与验收

- 只有授权 verifier / benchmark evaluator 能签发 `verified`；Agent 自述与工具退出码都是观测。
- 权威工件：`trace.jsonl`、`progress.json`、`result.json`、`world.*`；
  不可信侧：`events.jsonl`、`agent.events.jsonl`、`agent.summary.md`、`shared-findings.md`。
- 容器永远拿不到 run root、world 存储、结果、checkpoint、凭据；唯一新增的
  跨会话可写面是 `executor-shared/`，其边界由测试守住。
- **效果验收是比赛结果**（得分/名次）；确定性的 CI 护栏（信任分层、恢复对账、
  无开环子系统回归、import 方向、模块与行数上限）不计入效果验收。

相关文档：[agent-architecture-v2.md](agent-architecture-v2.md)、
[zc-architecture-comparison.md](zc-architecture-comparison.md)、[tsec.md](tsec.md)、
[security/execution-boundary.md](security/execution-boundary.md)、
[roadmap/planning.md](roadmap/planning.md)。
