# 当前架构与黑客松优秀架构对比（ZC 智能渗透闯入赛）

> 对标来源：[Yeti-791/Tsec-Hackathon](https://github.com/Yeti-791/Tsec-Hackathon)（腾讯云智能渗透黑客松
> 官方资源仓：两届线上前十战队的答辩 PPT、官方 API/MCP 文档与工具包）。
> 目标赛场：[zc.tencent.com/hackathon](https://zc.tencent.com/hackathon)（王座协议·智能渗透闯关赛）。
> 本文只做对比与规划，不改变现有信任边界；实现细节仍以
> [agent-architecture-v2.md](agent-architecture-v2.md) 为准（v2 已删除本文提到的 v1 认知脚手架）。
> 赛场侧的落地设计与 P0/P1 梯子见 [agent-engineering.md](agent-engineering.md)。

## 1. 对标对象

官方资源仓本身是一个导航/资料仓，真正可对比的"优秀架构"来自两届获奖战队的方案。抽取出的共同要素：

| 届次/名次 | 战队 | 架构要点 |
| --- | --- | --- |
| 第二届 #1 | ai 小分队（绿盟） | Manager + Solver + Observer 三层解耦；Observer 旁路监督不干预执行；RTK Rewrite 三层压缩对抗上下文腐败；Ralph-Loop 用系统状态约束结束判定；7 模型并行竞争上岗 |
| 第二届 #3 | Bytex（Cairn） | 黑板系统 + 蚁群算法 + 涌现行为；平等 Worker 动态抢任务，反对预定义角色分工；全场唯一 AK，成本 7692 元 |
| 第二届 #4 | ToBeNumberOne（京东） | "Less Structure More Intelligence"；资产树 / 接口深度建模（`depth:n`、`USES_API`、`CONTAINS`）驱动优先级 |
| 第二届 #5 | 奇盾明焰 | 主会话 + Advisor Agent（意图对齐，异步注入 PRIORITY/DO/DO NOT）+ KB Analyst Agent（公开 writeup 库 + 自学习库双路 RAG）；`.pentest-notes.md` 笔记 Agent |
| 第二届 #6 | 运行完成（中国电信） | skills 维护、知识库沉淀、断点续跑、模型无关；下一步：精细记忆压缩、按角色的异构模型路由 |
| 第二届 #7 | For Future / ChainReactors（aide） | 纯自然语言 FSM 执行引擎（ralphloop / explorer-execute / ant-colony 三种 Pattern）；overlay 注入领域 prompt；多执行器（claude-code / codex / opencode）；Task Coordinator / Team P2P / Craft 三种组织；"Less Than Nothing"——零领域知识是主动留白 |
| 第二届 #9 | 星空有云 | Dispatcher + 按方向的 Lead（Web/CVE/Network/AD） + op-recon/op-attack 子 agent + Monitor；DAST 平台 + 反连平台 + 中间人流量 |
| 第二届 #2/#8/#10 | Sniper / 清华 / 戴夫 | PPT 为图片版，未能提取文字，仅作名录 |
| 首届 #2/#3/#4/#6/#7/#8/#9/#15/#16/#17/#18 | xjtuHunter / BinX / Antix / NeuroSploit / ai 小分队 / D@WNEDG3 / yhy / 华科 / 瀚海星云 / 小白 / HRP | 见下方法线；关键可复用：D@WNEDG3 的多级队列 + 5 agent 竞赛 + 步数阈值分级提示；yhy 的顾问/主攻手双 Agent 与动态角色互换；Antix 的"单一 MCP 只暴露 Python 执行" Meta-Tooling；sickhack 的文件系统记忆层 + 上下文中间件；长亭外的 "Programming, not Prompting" MPI DSL；NeuroSploit 的 ReMem 自进化经验库 |

一句话概括获奖共识：**强 LLM × 少工程 × 强的上下文/知识/调度经济学**。工程重心不在"编排框架"，而在
(a) 抑制长轮次认知退化，(b) 让领域知识按需进入上下文，(c) 用步数/成本阈值做调度与止损，
(d) 多模型/多 agent 并行制造试错多样性。

## 2. 当前架构（red-harness）自述

现状是一个**证据驱动、可恢复的 Python 模块化单体**：Pi 拥有推理循环，Harness 拥有生命周期、执行
策略、世界状态、验证、预算、trace 与恢复。已落地能力：

- 闭环控制主脊 + 冻结的 import 闭包（`tests/test_architecture.py`）。
- 三层信任（claim < evidence < verified）、`VerifierRegistry` capability 签发裁决。
- 事件溯源 world、九相位生命周期、`RunCheckpoint` v2/v3、`reconcile_pending_actions` 恢复。
- 多维度预算（token/model/tool/cost/wall-time）软限 + 收敛 steer。
- ZC 原型：单 keeper 容器内 1 Manager + N Solver + 1 Observer，租约/预算份额/触发式 Observer。
- TSec 集成：`benchmark/tsec_client.py`（官方 SDK 包装）+ `TSecBenchmarkAdapter` + `harness tsec`。

## 3. 逐维度对比

| 维度 | red-harness 现状 | 获奖架构做法 | 判定 |
| --- | --- | --- | --- |
| 控制权与信任 | 唯一控制权威，验证/证据/裁决分级，Agent 输出恒为 claim | 多数直接把模型输出当结论，靠平台提交结果兜底 | **我们领先**，但对比赛得分不直接加分 |
| 恢复/幂等 | checkpoint + 对账 + run_action_owner flock | 仅 #6 把"断点续跑"列为未来项 | **我们领先** |
| 上下文压缩 | 有 round brief + `extract.py` 事实抽取；无会话级 compaction、无"重置+记忆回注" | RTK Rewrite 三层压缩；sickhack 在 30k/100k 触发上下文中间件；ai 小分队"笔记 + 状态重置 + 记忆回注" | **明显落后** |
| 领域知识/RAG | `skills/offensive` 仅元数据目录，Pi 自带 skills 关闭，无 KB | 双路 RAG（奇盾明焰）、PayloadsAllTheThings/HowToHunt（sickhack）、Nuclei 模板（For Future） | **明显落后** |
| 多模型 | 已落地同题多会话竞争：`AgentSpec.executors` 每题最多 4 个会话，可各配 provider / model / thinking / prompts（见 [agent-engineering.md](agent-engineering.md)） | 7 模型竞争、按角色/难度路由异构模型 | **已补齐** |
| 并行粒度 | `run_tsec_challenges` 支持最多 3 题并行（`--parallel`），每题独立 run dir 与预算；`min(wall_time, 3600)` 单题上限仍在 | 每题一 worker/agent group，平台 3 并发实例打满 | **已补齐**，墙钟上限仍待按赛窗放宽 |
| 调度经济学 | 单题内前沿轮转 + 停机优先级；`--hint` 全 run 一次性 | 多级队列 + 步数阈值；30 步后分级注入提示；难度与能力不对称时不看 difficulty 看步数 | **落后** |
| 防沉迷/防固化 | Observer 能检测重复动作签名，但只产 critique，不打断 Solver | 直接 hook 工具结果注入干预指令；同题获取 N 次强制清空笔记 | **半成品** |
| 工具面 | 容器内 Kali 全工具（read/bash/edit/write） | 同路线（yhy/Antix 均主张用真 Kali 而非堆 MCP） | **持平** |
| 代码组织 | 有架构边界测试、LOC 上限、控制脊闭包 | 多为脚本化单体 | **我们领先**，但也意味着改动成本更高 |

## 4. 关键结论

1. **并行度已经放在题目之间。** `run_tsec_challenges` 现在按 `--parallel`（上限 3）同时推进多道题，
   题内再由 `executors` 竞争，两级并行都用上了；剩下要盯的是机器容量（容器数 = 题数 × 会话数）
   与单题 `min(wall_time, 3600)` 这个仍与赛窗不匹配的硬编码上限。
2. **真正决定得分的是上下文与知识，而不是验证严谨性。** 我们的信任分层/恢复/预算对"稳"有价，但答案
   正确与否由平台 flag 判定；长轮次退化与"不知道 payload 怎么写"才是丢分主因。
3. **`--hint` 太粗。** 现在是一开全开；获奖做法是**按步数阈值分级**（先自己打，30 步未解才注入提示并
   接受扣分）。
4. **墙钟被硬编码腰斩。** `effective_budgets` 把单题 wall_time 压到 3600s，与比赛时段长度不匹配。

## 5. 计划（按 48 小时窗口排期）

> 时间基准：v1 原型文档记线上初赛为 10 月 17–18 日（当地 10/15），该文档已在 v2 中删除。以官方赛程为准，
> 若时间更宽裕，按 P0 → P1 → P2 逐段推进。

### P0 — 必须（Day 1）

| ID | 交付 | 验收 | 主要落点 |
| --- | --- | --- | --- |
| ZCP-01 | 赛制核对：确认本届实际接入面（官方文档写 `/api/*` + `Agent-Token`，仓库走 `tsec_benchmark` SDK 的 `openapi/v1`，平台另提供 `/mcp`），以及并发实例上限、3 req/s 限频、提示扣分与名次系数 | 对真实平台跑通 list/start/submit/close 全绿并记录端点 | `benchmark/tsec_client.py`、`preflight.py` |
| ZCP-02 | 客户端限速与实例生命周期：3 req/s 令牌桶；同时运行实例 ≤ 3，用完即 `close_challenge` 释放；提交沿用 `submitted_keys` 去重 | 离线计数测试 + 活体不触发 429；实例数不越界 | `benchmark/tsec_client.py`、`tsec_adapter.py` |
| ZCP-03 | 多题并行调度：`run_tsec_challenges` 由串行改为**最多 3 题并行**的任务池，每题独立 run dir 与预算 | 假平台脚本验证 3 题并行、实例数 ≤ 3、互不污染（已落地，见 `tests/test_tsec_adapter.py`） | `benchmark/tsec_adapter.py`、`benchmark/runner.py` |
| ZCP-04 | 止损与分级提示：per-challenge 步数/时间/成本阈值；步数阈值触发一次平台提示注入（替代全局 `--hint`），记录扣分影响 | 阈值触发不干扰其它题；提示仅注入一次 | `tsec_adapter.py`、`solver_policy.py` |

> 状态：ZCP-03 已落地；ZCP-01/02/04 未落地，赛前必须完成。
> 同题多会话竞争（原 PLN-04）已落地，见 [roadmap/planning.md](roadmap/planning.md)。

### P1 — 应当（Day 1–2）

| ID | 交付 | 验收 | 主要落点 |
| --- | --- | --- | --- |
| ZCP-05 | 上下文压缩与记忆回注：长度/轮次触发的会话级压缩，关键事实与笔记外置到 run 工件后重开 Pi 会话回注（对齐 RTK Rewrite / 状态重置） | 模拟超长会话，压缩后仍保留已确立事实、已排除路径与开放前沿 | `solver_loop.py`、`progress.py`、`pi_adapter.py` |
| ZCP-06 | 工具调用防沉迷：连续同命令族 N 次时**直接向 Solver 注入干预**（复用 `crew/observer.py` 的重复签名检测，改为可打断而非仅 critique） | 脚本化重复 `curl` 触发干预并解除循环 | `crew/observer.py`、`crew/runtime.py` |
| ZCP-07 | 轻量知识库：把 PayloadsAllTheThings / HowToHunt / OWASP CheatSheet 以只读目录暴露，prompt 引导"按需 ls/cat"（sickhack 路线），**不引入向量库** | KB 路径进入 agent 上下文且不越权、不越网 | `skills/`、`prompts/zc-solver.md`、`environment.py` |

### P2 — 可选（若时间允许）

- **ZCP-08 异构模型路由**：按角色（Manager/Solver/Observer）或按难度分配不同模型，扩展已有的
  `CrewRoleSpec.pi` 覆盖。
- **ZCP-09 多模型竞争上岗**：同题 N 个 Solver 用不同温度/模型，先出 flag 者提交（ai 小分队 + D@WNEDG3）。
- **ZCP-10 赛后复盘**：`harness audit-run` 汇总每题步数、成本、重复率、hint 使用与得分。

### 明确不采纳

- 黑板 + 蚁群涌现（Cairn）：与"唯一控制权威、无开环子系统"冲突，且 48h 内无法验证收益。
- 可编程 MPI DSL（长亭外）：与 planner 非目标冲突，工程量过大。
- 神经世界模型 / 向量数据库：`agent-architecture.md` 已列非目标。

## 6. 边界与风险

- **架构测试会挡路**：新增模块若进入 `runtime.bootstrap` / `benchmark.runner` / `solver_loop` 的 import
  闭包，必须显式登记 `CONTROL_SPINE`；模块数与源码行数上限由 `tests/test_control_metrics.py` 冻结。
  每次增删模块都要跑 `pytest tests/test_architecture.py tests/test_control_metrics.py -q`。
- **改动 egress 前先读** [security/execution-boundary.md](security/execution-boundary.md) 与
  [security/gateway-tools.md](security/gateway-tools.md)；KB 必须留在本地只读挂载，不得换取网权限。
- **接入面未证实**：仓库注释引用 `/openapi/v1/*`，官方第二届文档为 `/api/*` + `Agent-Token`，两者不一致。
  ZCP-01 必须先对真实平台核对，避免带着错误的客户端进入比赛时段。
- **不要为比赛牺牲信任边界**：`--allow-host-agent` 仍是信任闸门；平台 token 只在 Harness 侧。

## 7. 参考

- `docs/agent-architecture-v2.md`（当前设计：Agent-Native Harness）
- `docs/roadmap/planning.md`（PLN-04 同题竞争会话；其余规划条 v2 已 descope）
- `docs/tsec.md`（TSec 集成与消融矩阵）
- 官方 API/MCP 文档与工具包：`Tsec-Hackathon/第二届智能渗透黑客松/`
