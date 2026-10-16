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

| ID | 交付 | 落点 | 状态 | 验收 |
| --- | --- | --- | --- | --- |
| PLN-04 | 同题多 executor 竞争调度 | `src/harness/runtime/executors.py`、`src/harness/benchmark/runner.py` | 已落地 | `tests/test_executors.py`：一次 provision、重复候选只提交一次、首个成功取消其余会话 |
| ZCP-01 | 接入面核对（SDK `openapi/v1` vs 官方 `/api` + `Agent-Token`） | `src/harness/benchmark/tsec_client.py`、`src/harness/preflight.py` | 离线就绪；实时核对 blocked（需平台窗口） | 离线契约与 preflight 环境诊断已就绪（`tests/test_tsec_adapter.py`、[tsec.md](tsec.md) 核对清单）；对真实平台跑通 list/start/submit/hint/close 待执行 |
| ZCP-02 | 3 req/s 令牌桶 + 实例 ≤3 + 用完 `close_challenge` 释放 | `src/harness/benchmark/tsec_client.py`、`src/harness/benchmark/tsec_adapter.py` | 已落地 | 假时钟证明 3 req/s 不越界；实例注册表上限 3、重复启动拒绝、失败/关闭幂等释放；429 单次退避后 fail-closed（`tests/test_tsec_adapter.py`） |
| ZCP-03 | 多题并行调度（≤3 题，独立 run dir 与预算） | `src/harness/benchmark/tsec_adapter.py` | 已落地 | `run_tsec_challenges` 的信号量与按题 run dir（`tests/test_tsec_adapter.py`） |
| ZCP-04 | 按步数阈值分级提示，替代全局 `--hint` | `src/harness/benchmark/tsec_adapter.py`、`src/harness/solver_loop.py`、`src/harness/cli.py` | 已落地 | `--hint-threshold`（默认 0＝关闭）；`request_hint` 同题只取一次、失败不重试；连续无进展达阈值经 steer 注入一次（`tests/test_solver_loop.py`、`tests/test_tsec_adapter.py`）；`result.json` 记录 `hint_used`/`hint_trigger` |
| ZCP-11 | 墙钟上限解除：有效预算取声明的 `--wall-time`，另加运维安全阀 | `src/harness/benchmark/tsec_adapter.py`、`src/harness/cli.py` | 已落地 | 声明 7200s 时有效预算就是 7200s；`--max-wall-time-seconds` 只在显式传入时生效（`tests/test_tsec_adapter.py`） |

P1 是赛前有余力才做、赛后必做的项：

| ID | 交付 | 落点 | 状态 | 验收 |
| --- | --- | --- | --- | --- |
| ZCP-05 | 上下文重启与接力简报（阶段一已落地）；容器内钩子为阶段二，默认关闭、maintainer 已复核 | `src/harness/solver_loop.py`、`src/harness/benchmark/runner.py`、`src/harness/progress.py`、`src/harness/pi_assets/harness-context.ts` | 阶段一、二均已落地 | 阶段一：工具调用 300 次或再用掉 60% 工具调用预算时重启，最多 2 次；简报含已确立事实/已排除路径/开放意图/指针；旧会话归档为 `restart-N`（`tests/test_solver_loop.py`、`tests/test_executors.py`）。阶段二：`pi.harness_extension` 显式开启才安装 Harness 扩展并停传 `--no-extensions`；扩展仅 `tool_result` 落盘（32k/600 预览）与 `session_before_compact` 保留清单两个钩子，失败回退默认压缩（`tests/test_pi_adapter.py`；边界见 [security/execution-boundary.md](security/execution-boundary.md)）；Pi 模型实测 blocked（本机无模型凭据） |
| ZCP-06 | 重复动作打断（消费已有的 `args_family`，连续同族注入干预） | `src/harness/progress.py`、`src/harness/solver_loop.py` | 已落地 | 同族连续 3 次触发一次干预、6 次升级为收敛级；确认进展清零计数；不同命令族不误伤（`tests/test_solver_loop.py`） |
| ZCP-07 | 知识库填充（技能手册 + 自研索引 + 上游语料按需挂载） | `skills/offensive/`、`knowledge/README.md` | 已落地 | 7 个技能含全部必需小节且 ≥50 行；索引链接可解析；上游语料只经 `scripts/fetch-knowledge.sh` 按 pinned revision 拉取（`tests/test_knowledge_catalogue.py`） |
| ZCP-08 | 失败/已排除路径看板 | `src/harness/world/context.py` | 已落地 | refuted/superseded 出现在 Ruled out（上限 10 条，截断给计数）；无记录时投影不含该段（`tests/test_world_context.py`） |
| ZCP-09 | 会话级用量归因与 prompt/model 战绩 | `src/harness/benchmark/runner.py`、`src/harness/cli.py` | 已落地 | `executor_summary` 按 (prompt, provider, model) 聚合；单会话结果不新增键（`tests/test_executors.py`） |
| ZCP-10 | 收尾加固（结构化摘要 + 缺失可观测 + 复用上一轮摘要） | `src/harness/solver_loop.py`、`src/harness/cli.py` | 已落地 | `result.json` 的 `agent_report` 记录摘要是否存在与缺失段落，并写 `solver.summary_incomplete`；`--seed-notes-from` 把上一轮摘要放进新会话 notes（`tests/test_executors.py`） |
| ZCP-12 | 重启简报质量观测（词数、来源 notes、缺失段落；只观测不排序） | `src/harness/benchmark/runner.py`、`src/harness/solver_loop.py` | 未落地 | 每次重启写会计级指标；无重启时结果不含新键（`tests/test_executors.py`） |
| ZCP-13 | 会话级收尾模板复用（重启/取消路径复用四段摘要，摘要仍非证据） | `src/harness/solver_loop.py`、`src/harness/cli.py` | 未落地（条件项） | 重启/取消路径的缺失段落可观测（`tests/test_solver_loop.py`） |
| ZCP-14 | 知识语料扩容（pinned 取数＋索引＋许可） | `knowledge/`、`skills/offensive/`、`scripts/fetch-knowledge.sh` | 未落地 | 目录结构校验通过；运行期无网络依赖（`tests/test_knowledge_catalogue.py`） |

排期与实现顺序见 [zc-improvement-design.md](zc-improvement-design.md)：A 段（ZCP-11/05/06/07）与
B 段（ZCP-08/09/10）已落地，C 段（容器内 Pi 扩展）在 maintainer 复核通过前不动。
赛制接入面仍按 ZCP-01/02 先行——不核对接入面就没有后续的一切。
≤5 工作日窗口的排期、依赖与验收口径见 [zc-improvement-plan.md](zc-improvement-plan.md)；ZCP-13 是 D3 门后启动的条件项。

## 6. 信任边界与验收

- 只有授权 verifier / benchmark evaluator 能签发 `verified`；Agent 自述与工具退出码都是观测。
- 权威工件：`trace.jsonl`、`progress.json`、`result.json`、`world.*`；
  不可信侧：`events.jsonl`、`agent.events.jsonl`、`agent.summary.md`、`shared-findings.md`。
- 容器永远拿不到 run root、world 存储、结果、checkpoint、凭据；唯一新增的
  跨会话可写面是 `executor-shared/`，其边界由测试守住。
- **效果验收是比赛结果**（得分/名次）；确定性的 CI 护栏（信任分层、恢复对账、
  无开环子系统回归、import 方向、模块与行数上限）不计入效果验收。

相关文档：[agent-architecture-v2.md](agent-architecture-v2.md)、
[zc-improvement-design.md](zc-improvement-design.md)、
[zc-improvement-plan.md](zc-improvement-plan.md)、
[zc-architecture-comparison.md](zc-architecture-comparison.md)、[tsec.md](tsec.md)、
[security/execution-boundary.md](security/execution-boundary.md)、
[roadmap/planning.md](roadmap/planning.md)。
