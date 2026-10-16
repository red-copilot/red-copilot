# 智能体架构 v2：Agent-Native Harness

> 本文件取代 v1 设计（原 `agent-architecture.md`、`control-theory.md`、
> `agent-engineering.md` 与 ZC 多角色原型 `zc-agent-prototype.md`）。
> 模块边界与 import 规则的强制项在 `tests/test_architecture.py`。

## 1. 定位

v1 让 Harness 参与认知：它把技能元数据投影成"意图-事实前沿"，用确定性规划器挑选候选，
再用 Manager/Solver/Observer 角色编排把工作分派出去。实践对照（两届腾讯云智能渗透黑客松
获奖方案）显示，得分瓶颈不在编排严谨性，而在长轮次认知退化、领域知识进不了上下文，
以及并行度放错位置。

v2 把这层脚手架删掉，只留下一句话：**Harness 供给环境与信任，模型拥有认知。**

| 层 | 归属 | 内容 |
| --- | --- | --- |
| 认知（做什么/怎么做） | 模型 | 推理、规划、选工具、写笔记、读知识 |
| 环境（在哪儿做） | Harness | 容器、只读任务与知识挂载、可写工作区、工具与网络 profile |
| 信任（算不算数） | Harness | 三层信任、授权裁决、证据引用 |
| 记账（花了多少） | Harness | 预算、trace、工件、checkpoint |
| 恢复（崩了怎么办） | Harness | 九相位生命周期、对账、单操作者 flock |

非目标：黑板/蚁群等涌现式协调、可编程 DSL、向量检索、神经世界模型、把模型输出当结论。

## 2. 认知层：文件系统 + 通用 coding agent

Agent 的全部"认知基础设施"就是文件系统里的 markdown：

- **笔记** `$HARNESS_NOTES_DIR`（容器内 `/run/harness/notes`）：Agent 自己写、自己读。
  跨轮次、跨会话的长期记忆。Harness 不解析、不排序、不做 schema 校验。
- **知识** `$HARNESS_KNOWLEDGE_DIRS`：只读 markdown 目录（`skills/offensive`、
  `knowledge/`、payload 手册、方法论）。Agent 用 `ls` / `cat` 按需取用，不预先全量注入。
- **世界** `$HARNESS_WORLD_CONTEXT`：Harness 投影的**事实**视图——焦点集、已确立事实、
  未解未知、失败记录、预算。它是观测输入，不含候选动作、不含计划。

技能目录 `skills/**/<skill-id>/SKILL.md` 只描述"何时适用、预期结论、操作边界"，
**没有可执行语义**：没有任何模块解析它，也没有排序器把它变成候选列表。需要强制执行的东西
（授权范围、预算、破坏性动作、验证权威）留在 Harness，不放进目录。

这层刻意留白（对齐 For Future 的 "Less Than Nothing" 与 Antix 的
"只暴露一个执行入口"）：工程做得越少，越少挤压模型的推理空间。

## 3. 执行层：Pi 优先 + 可插拔 executor

- `type: pi` 是默认执行内核（容器内 Pi RPC），保持既有硬化：只读根文件系统、
  `--cap-drop ALL`、no-new-privileges、无 docker socket、只读 `/task` 与 `/knowledge/*`、
  可写 `/run/harness`。
- `ExecutorSpec` 声明同一道题上并行的多个 solver 会话（不同 provider / model / thinking /
  `extra_args` / system prompt）。`type: cli` / `type: docker` 复用既有适配器契约，
  因此外部 coding agent 走同一 `build_agent_adapter` 入口。
- 并行度：**最多 3 道题并行**（平台并发实例上限），**每题可由多个 executor 竞争**。
  竞争单元共享一次 provision（同一个靶机实例）与同一 `submitted_keys` 去重集合，
  任一会话提交成功即结束该题。

## 4. 控制律与边界

`solver_policy.decide_solver_action` 退化为**纯止损律**：无进展上限、无确认进展上限、
预算耗尽、低预算且无进展。没有 replan、没有候选打分、没有计划文件。

保留的 Harness 权威：

- 只有授权 verifier / benchmark evaluator 能签发 `verified`；Agent 自述与工具退出码都是观测。
- 权威工件仍是 `trace.jsonl`、`progress.json`、`result.json`、`world.*`；不可信侧仍是
  `events.jsonl`、`agent.events.jsonl`、`agent.summary.md`。
- checkpoint 只记录控制游标与已用量，不含角色/计划字段（v3 记录仍可读）。

## 5. 接口

| 变更 | 内容 |
| --- | --- |
| 删除 | `--solver-profile`、`--crew-profile`、`--crew-solvers`、`--max-replans`、`--max-unconfirmed-progress`、`harness validate-skill` |
| 删除 | `AgentSpec.crew` 与 `CrewSpec`/`CrewRoleSpec`/`CrewBudgetShares`/`CrewLimits`；typed events `crew.plan`/`crew.critique`；`SkillSpec` 与 `StateSelector`、`Goal.required_states` |
| 删除 | 模块 `planner.py`、`cognition.py`、`skills.py`、`crew/`、`runtime/solver_profile.py` |
| 新增 | `AgentSpec.knowledge: list[str]`（只读知识根，容器内挂到 `/knowledge/<name>`） |
| 新增 | `AgentSpec.executors: list[ExecutorSpec]`（同题多会话竞争） |
| 新增 | CLI `--knowledge-root DIR`（可重复，覆盖 YAML）、`harness tsec --parallel N`（默认 1，上限 3） |
| 新增 | env `HARNESS_NOTES_DIR`、`HARNESS_KNOWLEDGE_DIRS`；工作区 `notes/` |
| 新增 | CLI `harness tsec --max-wall-time-seconds N`（运维安全阀，默认不设：声明的 `--wall-time` 就是有效赛窗） |
| 新增 | CLI `harness tsec --seed-notes-from DIR`：把上一轮 `agent.summary.md` 作为只读接力材料写进新会话 notes |
| 新增 | 长会话重启：自上次重启起工具调用 300 次或又用掉 `max_tool_calls` 的 60% 时触发，最多 2 次；接力简报写在会话根的 `carry-forward.md`，事件为 `solver.context_restart` |
| 新增 | 结果契约：`agent_report.present` / `agent_report.sections_missing`；竞争运行的 `executor_sessions[].prompt`、`provider`、`summary_sections_missing` 与顶层 `executor_summary` |
| 新增 | trace 事件 `solver.repetition_steer`、`solver.context_restart`、`solver.summary_incomplete` |

## 6. 消融基线

v1 的意图-事实前沿没有删除历史，而是冻结为离线基线：实现位于 `tests/ablation/`
（不进包、不进 control spine、不上生产路径），回归在
`tests/test_ablation_frontier.py`。同一 snapshot 与 skills 仍必须产出同一个确定性前沿
（目标直击优先、后向链补前置、无目标声明时退化为全部 enabled）。

## 7. 验收

- **效果验收：比赛结果**。得分/名次是唯一标准；赛前取不到数时，以端到端 demo run
  （多 executor 竞争 + 3 题并行）作为临时证据。
- **不变量护栏（CI 强制，不计入效果验收）**：信任分层、恢复对账、无开环子系统回归、
  import 方向、模块/LOC 上限。

相关文档：[architecture.md](architecture.md)、[world-state.md](world-state.md)、
[pi.md](pi.md)、[tsec.md](tsec.md)、[security/](security)、[roadmap/README.md](roadmap/README.md)。
