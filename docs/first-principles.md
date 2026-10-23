# Red Harness 第一性原理

> 状态：工程基线。日期：2026-10-24。本文不是产品宣传，也不是当前实现的完成证明；它用于在
> 设计评审、重构、评测和故障复盘中裁决“什么该由 Harness 做、什么必须由 Agent 做、什么根本
> 不该做”。

## 0. 一句话

**Red Harness 是一个受控、可审计、可评测的安全智能体运行时：Agent 在有限权限和有限资源下
探索不可信环境，Harness 负责隔离、授权、因果记录、恢复和独立验证，而不是替 Agent 思考。**

## 1. 方法论与来源

本文采用“前提 -> 不变量 -> 架构 -> 验收证据”的推导方式：

1. 先写出不可再简化的领域前提；
2. 从前提推导出无论实现语言和模型版本如何都必须成立的不变量；
3. 只在不变量要求的地方设计确定性机制；
4. 用负面测试、故障注入、回放和真实评测证明机制成立；
5. 模型能力、平台约束或运行数据变化后，重新审视并删除不再承重的复杂度。

主要经验来源：

- [Anthropic Engineering](https://www.anthropic.com/engineering)；
- [Building effective agents](https://www.anthropic.com/research/building-effective-agents)：
  先用最简单的方案，只有能证明改善结果时才增加复杂度；
- [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)：
  context 是有限注意力预算，应保留最小的高信号集合，并用结构化工件跨越上下文窗口；
- [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)：
  长周期任务需要增量推进、进度工件、可恢复环境、端到端自检和干净交接；
- [Harness design for long-running application development](https://www.anthropic.com/research/harness-design-long-running-apps)：
  harness 组件是对“模型当前做不到什么”的假设，模型升级后要重新压力测试并删除过时假设；
- [Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)：
  agent 评测需要真实任务、trial、grader、transcript、outcome，以及多种评测层组合。

这些文章是经验材料，不是认证标准。Red Harness 面向不可信安全目标和外部副作用，不能机械复制
面向应用生成的 harness 结构。

## 2. 项目目标

### 2.1 主要目标

在对抗性、部分可观测、带外部副作用的环境中，让安全智能体的运行：

- **可执行**：环境、权限、网络、工具和预算被明确声明；
- **可审计**：动作、观测、证据、裁决和恢复有可追溯记录；
- **可评测**：成功由独立 evaluator/verifier 判定，而不是由 Agent 自述；
- **可恢复**：崩溃和未知副作用不会被静默重放；
- **可比较**：模型、prompt、工具和 harness 变化可以在受控条件下比较；
- **可维护**：复杂度有明确职责、测试和删除条件，不因模型升级而变成永久脚手架。

### 2.2 成功标准

项目的最终效果标准是真实安全任务和 benchmark 的结果，而不是架构图、代码行数或单元测试数量。
至少同时观察：

- 成功率、得分、完成率；
- 首次提交时间与提交有效率；
- token、模型调用、工具调用、成本和墙钟；
- steer 数量、无效 steer 比例、unknown action 数量；
- 崩溃恢复率、重复计费/重复副作用次数；
- 多 trial 方差和基础设施噪声；
- trace/audit 的可理解性与人工复核成本。

### 2.3 非目标

- 不做通用 agent 框架；
- 不把 Harness 变成 planner、技能排序器、任务 DAG 引擎或角色编排器；
- 不追求 exactly-once；
- 不把哈希链宣传成恶意宿主的密码学证明；
- 不承诺 LLM 行为逐字节复现；
- 不为了“统一”把所有类型都塞进一个巨型事件模型；
- 不把 benchmark 专属生命周期写进 kernel；
- 不在核心中引入微服务、消息队列、向量数据库或神经世界模型。

## 3. 不可再简化的前提

### P1. Agent 是探索者，不是事实来源

模型会误报、跳过步骤、重复调用、误判成功，并可能被任务内容或工具输出诱导。Agent 的文本、
计划、置信度和自评都只能作为 claim 或输入，不能直接成为 evidence 或 verified。

**推论：** Agent 可以自由读取知识、写笔记和选择工具，但不能签发目标结论。

### P2. Harness 是边界，不是大脑

Harness 不能让模型变得更聪明；它只能限制模型能做什么、记录实际发生了什么、在什么条件下必须
停止，以及哪些证据足以改变结论。

**推论：** Harness 的扩展理由必须是授权、隔离、记账、验证、恢复或可观测性，而不是“帮助模型
规划”。

### P3. 真相有生产者，事实分层

Claim、Observation、Evidence、ObjectiveVerdict 是不同层次的事实：

```text
Agent 声称     -> claim
授权观察者看到 -> observation
验证器绑定命题 -> evidence
授权评测者结论 -> verdict
```

工具退出码、submission accepted、Agent 总结和目标完成都不等价。

**推论：** 没有独立 observation 时，宁可得不到 verified，也不能猜测成功。

### P4. 外部副作用不可逆，且可能未知

请求一旦发出，进程崩溃并不证明副作用没有发生。超时、连接中断和容器被杀都可能留下未知状态。

**推论：** unknown action 不得自动重放；必须查询、独立观测或人工对账。无法确认时，Run 应带着
unknown IDs 关闭，而不是伪造成功。

### P5. 环境是实验的一部分

容器、网络、工具版本、目标状态、平台限流和基础设施噪声都会改变 agent 表现。把 agent 放在
不同环境中比较，得到的不是 harness 效果。

**推论：** 隔离、镜像、网络 profile、超时、资源限制和平台健康状况必须显式记录；评测要控制或
报告基础设施噪声。

### P6. Context 是有限资源

上下文窗口再大也会被噪声污染。长任务不可能只依赖对话历史；需要把重要状态外置为 Agent 可读
的笔记、进度、事实投影和结构化交接。

**推论：** Harness 应提供有界、高信号的事实视图和恢复指针，但不应替 Agent 编写计划。

### P7. 复杂度必须支付租金

每个 harness 组件都编码了一个“模型当前做不到什么”的假设。假设可能错误，也会随模型升级过期。

**推论：** 新增组件必须说明目标失败模式、预期收益、评测方法和删除条件。不能因为架构上优雅而
永久保留。

### P8. 可复现不等于行为完全一致

LLM 采样、远端目标、时序和基础设施都会引入方差。真正可复现的是输入、环境、控制规则、artifact、
trace 和裁决链，以及从这些记录重建实验的能力。

**推论：** 记录必须足以 replay/audit；不能把一次模型输出的偶然一致性当成可复现性。

### P9. 恢复和审计是产品功能

崩溃、超时、未知动作和恢复后的对账不是运维边角料，而是运行时的核心语义。

**推论：** 恢复协议必须有故障注入和负面测试；checkpoint 只是提示，不能比账本更新的事实更强。

### P10. 评测是工程闭环，不是发布前一次性工作

Agent 是多轮、带工具、会改变环境的有状态系统。单元测试只能证明代码契约，不能证明任务效果。

**推论：** harness 变更至少需要离线 fixture、集成测试、故障测试；影响 agent 行为的变更还需要真实
benchmark、多 trial 和 trace 复核。

### P11. 模型能力会变化，Harness 必须可退休

今天的必要脚手架，可能是明天的上下文污染。模型升级后应重新测试既有假设。

**推论：** 删除过时组件与新增能力同等重要；架构应支持 shadow、fallback、ablation 和逐层替换。

### P12. 操作者是方向输入，不是目标证据

操作者 hint 可以改变探索方向，但不能替代 verifier/evaluator 的结论。凭据和 operator control
必须留在 Agent 不可写的边界。

**推论：** hint 是可信方向输入，不是 verdict；任何可被容器伪造的 operator 通道都不再可信。

## 4. 从前提推导出的不变量

| ID | 不变量 | 违反后的典型症状 | 验收证据 |
| --- | --- | --- | --- |
| I1 | 每个 Run 只有一个权威事实源 | 多个文件互相矛盾，事后靠人工解释 | 账本/权威存储的写入口测试 |
| I2 | 跨边界动作先有因果身份，再有副作用 | 无法回答“谁授权、是否发出、能否重试” | 接纳、dispatch、unknown/reconcile 测试 |
| I3 | unknown 不自动重放 | 重复提交、重复破坏、重复计费 | 五个崩溃窗口故障注入 |
| I4 | 信任不升级 | Agent 文本或工具退出码变成 verified | 伪造 producer/跨 run evidence 负面测试 |
| I5 | 投影可重建 | 删除派生文件后无法恢复视图 | 删除投影后从权威事实重建 |
| I6 | 控制律不含认知 | 重复命令、无进展、上下文长度触发 steer/stop | 反例测试：诊断指标不能改变控制决策 |
| I7 | 适配器不拥有真相 | Pi/TSec/Docker 直接决定 Run 成功 | adapter contract 与 architecture test |
| I8 | 失败关闭 | 隔离不可用、schema 错误、证据缺失时继续运行 | 不可用/损坏输入测试 |
| I9 | Agent 容器不能写权威状态 | Agent 可改 world/result/evidence/credentials | 挂载、权限与路径边界测试 |
| I10 | benchmark 不得污染核心 | kernel 出现 TSec 特例 | 依赖方向、control-spine 与 import 测试 |
| I11 | 预算先预留后结算 | 超限请求仍 dispatch，崩溃后重复计费 | reservation/settle 故障测试 |
| I12 | Context 有界且高信号 | 原始工具流水挤掉事实和失败记录 | token 上限、稳定渲染、golden context |

## 5. 职责边界

| 角色 | 拥有 | 不拥有 |
| --- | --- | --- |
| Agent | 推理、规划、选工具、写笔记、读取知识 | 目标 verdict、证据权威、宿主凭据、权威状态写入 |
| Harness | 环境、授权、预算、因果记录、生命周期、恢复、投影 | 替 Agent 规划、给 skill 排序、编造成功 |
| Verifier/Evaluator | 独立 observation、evidence、objective verdict | 获取 Agent 未暴露的宿主秘密、绕过权限边界 |
| Benchmark Adapter | 外部生命周期、提交、平台评测的翻译 | 修改 kernel、定义通用成功语义 |
| Operator | hint、取消、对账、人工判断方向 | 把 hint 当 objective verdict、绕过审计 |
| Projection | 从权威事实重建 World、Context、Trace、Result、Audit | 反向修改权威事实 |

## 6. 架构映射

### 6.1 控制闭环

```text
objective -> agent action -> isolated environment -> observation
    ^                                                   |
    |                                                   v
    +---------- bounded control law <- evidence/verdict-+
```

- Agent 负责选择下一步；
- Harness 只根据 deadline、预算、lease、operator cancel、objective verdict 和 unsafe unknown
  等外部可判定条件控制；
- 验证器/evaluator 独立判定目标。

### 6.2 认知层

- 笔记由 Agent 自己写、自己读，Harness 不解析为计划；
- knowledge 是只读 markdown，Harness 不排序、不注入候选动作；
- World Context 只投影事实、焦点、失败和预算，不投影“下一步建议”；
- 长任务通过 context reset、进度工件和 carry-forward brief 跨越窗口，而不是扩大对话历史。

### 6.3 因果层

- Run、Attempt、Action、Observation、Evidence、Verdict 有稳定身份；
- 跨宿主动作和模型调用在 dispatch 前接纳并预留预算；
- 容器 lease 内的 shell 动作由 lease 授权，观测后补 action identity；
- unknown action 不重放，必须对账；
- 当前 v2 仍是生产读路径，v3 从影子账本逐步迁移。

### 6.4 证据层

- verified 必须有独立、不可变、可追溯的 evidence；
- Agent claim 只能作为 claim；
- submission accepted 只是反馈；
- objective status 与 Run 终态分开；
- 审计检查 producer、run/action ID、时间、revision 和 artifact hash。

### 6.5 执行层

- Pi 在受限 Docker 中运行，只读 rootfs/task/knowledge，仅 workspace 可写；
- run root、账本、result、checkpoint、evidence、verifier logs 不挂载；
- 网络 profile 是 lease 的一部分，不能中途隐式放宽；
- 真实 provider/benchmark 凭据留在宿主侧 Gateway/adapter；
- 不可用隔离失败关闭。

### 6.6 评估层

- benchmark adapter 接入在 core 外；
- evaluator 是目标裁决者，不是 agent 自评；
- 同一 candidate 去重；
- 多会话是 Run 级搜索，不是多个真相源；
- 结果、trace、summary 和 grader 输入均可回放。

## 7. 工程方法

### 7.1 标准迭代环

1. **读真实 trace。** 先找失败模式，不从抽象图开始。
2. **写假设。** “模型做不到 X”或“环境会以 Y 方式失效”，并说明它如何影响结果。
3. **写可证伪的测试。** 先写失败测试、负面测试或故障注入；不能先写完机制再补测试。
4. **实现最小变更。** 优先删除、组合已有边界或增加一个窄端口，不先加新层。
5. **跑离线验证。** 目标测试、架构测试、全量测试、lint。
6. **跑真实评测。** 多 trial，记录 success、成本、时间、steer、unknown 和方差。
7. **决定保留/删除。** 有收益就保留并补文档；无收益就回滚并记录证据。
8. **更新原则和文档。** 只有证据改变了原来假设时才修改原则。

### 7.2 变更分级

| 变更 | 最低验证 |
| --- | --- |
| 文档、注释、非行为性重命名 | docs consistency、link/path 检查、lint |
| 纯 domain/contract 变更 | 目标单测、负面测试、architecture、全量 pytest |
| 影响 Agent 行为的 context/prompt/工具变更 | 离线 fixture + 真实 benchmark + trace 复核 + 多 trial |
| 影响隔离、凭据、网络、verifier 的变更 | 威胁模型、负面测试、故障注入、maintainer review |
| 影响评估语义的变更 | grader 校准、outcome/transcript 区分、baseline 对比 |

### 7.3 复杂度准入问题

任何新模块、新状态文件、新 prompt 层或新编排层，合并前必须回答：

1. 它补偿的是哪个可观察到的模型/环境限制？
2. 这个限制在目标模型和真实环境中仍存在吗？
3. 它改善哪个指标，改善幅度是多少？
4. 它引入了什么新的失败模式和信任边界？
5. 有无更简单的 prompt、工具或现有组件替代？
6. 何时删除？删除条件是什么？
7. 如果模型升级，如何 ablation 或 shadow 验证它不再承重？

四问答不出来，默认不合并。

## 8. 证据等级与完成定义

| 等级 | 证据 | 能证明 | 不能证明 |
| --- | --- | --- | --- |
| E0 | 静态检查、lint、架构解析 | 代码形状与边界 | 行为正确性 |
| E1 | 单元测试、负面测试 | 局部契约和失败关闭 | 系统集成 |
| E2 | 集成测试、故障注入、replay | 生命周期、恢复、审计 | 真实任务效果 |
| E3 | 真实 benchmark、多 trial、trace 复核 | 对目标任务的可用性 | 跨域泛化 |
| E4 | 持续 sweep、生产观测、回归对照 | 稳定性和操作价值 | 永恒正确性 |

“完成”不是“测试全绿”。完成必须说明它处于哪个证据等级、还缺什么。

## 9. 设计决策规则

- **能交给 Agent 的，不写进 Harness。** 规划、探索、工具选择和笔记结构属于 Agent；除非错误会破坏真
  相、安全、预算或恢复。
- **影响真相的，必须结构化。** 证据、verdict、身份、预算和 unknown 不能靠 prompt 约定。
- **没有观测，就没有 verified。** 允许 unavailable/inconclusive，不允许猜测。
- **unknown 优先于重试。** 先对账，再决定。
- **单写入口优先于多文件同步。** 多投影可以存在，但只能有一个权威来源。
- **结构化输入优先于自由文本解析。** 对 Agent 输出用 schema、限额和 fail-closed。
- **行为评测优先于架构辩论。** 先跑 trace 和 eval，再讨论抽象。
- **删除优先于叠加。** 保留每个组件的成本很高；能被模型或更简单机制替代的组件应退休。
- **局部实验优先于大爆炸迁移。** 用 shadow、feature flag、fallback 和逐阶段删除条件。
- **失败关闭优先于可用性。** 隔离、证据或账本不可用时，不得猜测成功。

## 10. 反模式

- Harness 侧 planner、DAG、技能排序或角色扮演；
- 把 Agent 自评、置信度或工具 exit code 当目标证据；
- 用“重复命令”“无进展”“上下文长度”自动 steer；
- 崩溃后重发未知外部副作用；
- submission accepted 直接当 objective verified；
- 把 raw tool output 无限写入 context；
- 为统一而引入分布式事件总线、消息队列或微服务；
- benchmark 特例进入 kernel；
- 只有一个巨大 YAML/prompt 而没有测试、回放和删除条件；
- 宣称哈希链能证明恶意宿主不可篡改；
- 宣称同一 prompt 必然产生相同 LLM 行为；
- 用单元测试通过代替真实 agent 效果评估。

## 11. 重新审视原则的触发器

- 模型升级或 provider 行为变化；
- 新 benchmark/platform 约束；
- 真实 sweep 显示新失败模式；
- 同一类失败连续出现；
- 预算、恢复或隔离测试出现回归；
- 基础设施噪声超过实验效果；
- 组件连续多个版本没有正向收益；
- 现有证据与本文前提冲突。

## 12. PR / 设计评审清单

- [ ] 这个变更解决哪个真实失败模式？
- [ ] 它属于 Agent、Harness、Verifier、Adapter 还是 Projection？
- [ ] 它增加了权威来源、状态文件或控制路径吗？
- [ ] 它会不会让 Agent 输出升级为 evidence/verdict？
- [ ] crash、unknown、超时和重复调用时行为是什么？
- [ ] 预算、隔离、网络和凭据边界是否仍然成立？
- [ ] 有没有先写失败测试和负面测试？
- [ ] 是否跑了目标测试、架构测试和全量测试？
- [ ] 是否跑了真实 eval 或多 trial？如果没有，为什么？
- [ ] 组件收益、成本和删除条件是什么？
- [ ] 文档是否区分“已实现”“已验证”和“待验证”？

## 13. 当前应用与证据缺口

当前生产读路径仍是 v2；v3 因果账本处于 M1 shadow，Stage 0-1 已增加
`LedgerAppender`、原子 batch append、严格 trace envelope 校验和多会话 golden fixture。它们是
正确的边界，不等于完整因果保证。

尚未闭合的关键证据：

- 真实 TSec sweep 和最终 evaluator 复跑；
- Docker-backed 五窗口崩溃矩阵；
- action admission、预算预留与 unknown reconciliation；
- Observation 到 action 的显式因果绑定；
- World/Context/Progress/Checkpoint/Result 的账本重建；
- 多 trial 方差与基础设施噪声分析。

在这些证据完成前：

- 不能说 v3 已成为生产架构；
- 不能把 533 个通过的测试等同于 agent 效果已提升；
- 不能声称 shell 动作可逐命令前置验证；
- 不能把 unknown 自动重试包装成可靠性。

## 14. 结语

Red Harness 的核心不是“让模型更自主”，而是让自主性发生在可授权、可观测、可恢复、可验证的
边界内。

> **模型可以自由探索，但不能自由定义事实、权限、成本和成功。**

这句话是项目的最终裁决原则。任何实现如果违反它，就不应进入 control spine。
