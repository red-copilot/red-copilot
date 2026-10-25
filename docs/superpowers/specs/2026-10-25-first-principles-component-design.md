# 第一性原理组件与模块设计 v3.1

> 状态：待审批设计稿。目标来源为
> [第一性原理架构 v3](2026-10-22-first-principles-architecture-design.md) 和
> [因果内核增量重构设计](2026-10-23-causal-kernel-incremental-refactor-design.md)。
> 本文不改变 v2 的现行生产读路径，只把“满足第一性原理的组件与模块边界”收敛到可实施粒度。
>
> 设计基线：`5c79f91`。当时控制脊为 60 个模块、16,983 行，架构/指标/文档目标测试通过。

## 1. 意图与为什么

项目已经正确地把认知留给 Agent，把环境、授权、记账、验证与恢复留给 Harness。下一步不应继续
按文件或框架分层，而应按**不可变事实和生产关系**分层。

系统的最小本质是：

```text
Run = 一条只追加因果流 + 有限状态机 + 外部效果适配器 + 可重建投影
```

因此，组件设计的成功标准不是“模块更多、边界更细”，而是：

1. 一个 Run 只有一个权威写入面；
2. 每个跨边界效果在 dispatch 前有身份、授权和预算语义；
3. 副作用状态未知时，系统不会猜测成功或自动重放；
4. 任何 `verified` 都能沿 Verdict -> Evidence -> Observation -> Action -> Attempt -> Run 追溯；
5. 删除 World、Context、Progress、Result、Trace、Checkpoint 后可以从账本重建；
6. Agent、benchmark、模型、Pi、Docker、FastAPI 都不能拥有 Run 真相；
7. 可以删除旧权威路径，而不是永久维护两套语义。

## 2. 目标、非目标与反目标

### 2.1 目标

- 设计一个可落地的七层组件模型：入口、应用、内核、控制、证据、投影、适配器。
- 为每个模块规定唯一职责、可写状态、禁止依赖和验收证据。
- 把 Run、Attempt、Action、Observation、Claim、Evidence、Verdict 变成稳定语义。
- 用动作多维状态替代一个含糊的通用 `ActionVerdict`。
- 用预算预留替代事后统计。
- 用账本投影替代多份权威状态文件。
- 给出可删除旧实现的纵向迁移切片，而不是长期双写。
- 保持 modular monolith、离线测试和现有公共读取契约的兼容窗口。

### 2.2 非目标

- 不引入微服务、消息队列、分布式事务或跨主机 worker；
- 不承诺 exactly-once；
- 不提供逐 shell 命令的前置审批；
- 不把哈希链宣传为恶意宿主无法伪造的密码学证明；
- 不在 Harness 中恢复 planner、技能排序、DAG 或角色编排；
- 不把 benchmark 专属生命周期写进 kernel；
- 不为减少文件数把无关职责塞进巨型模块。

### 2.3 反目标

以下情况即使测试全绿也判为设计失败：

- 新增模块只做转发，权威来源数量没有下降；
- 账本已经可用，却永久保留 v2 与账本双写；
- 用 producer 字符串而不是类型化能力签发 Evidence/Verdict；
- 为了恢复成功自动重放 `unknown` 副作用；
- 把模型返回、工具退出码或 submission accepted 当作 objective verified；
- 通过把状态塞进一个大 event 或大 service 来制造“统一”的假象。

## 3. 不可再简化的前提与不变量

### 3.1 前提

1. Agent 不可信且会犯错，其输出只能作为 claim 或探索输入。
2. 环境只能通过授权观察者实际捕获的 observation 被认识。
3. 外部副作用不可逆，进程崩溃不等于副作用没有发生。
4. Claim、Observation、Evidence、Verdict 有不同生产者，不能互相替代。
5. 可写缓存会漂移，派生文件不能反向修复真相。
6. 模型负责探索，确定性代码负责权限、预算、因果、恢复和终止。

### 3.2 不变量

| ID | 不变量 | 强制点 |
| --- | --- | --- |
| I1 | 每个 Run 只有一个权威事实源 | 只有 kernel store 可以追加权威事件 |
| I2 | 跨边界动作先有因果身份，再有副作用 | model/brokered/verifier 调用必须经过 admission |
| I3 | unknown 不自动重放 | recovery 只追加 unknown/reconcile，不重新 dispatch |
| I4 | 信任不升级 | Evidence/Verdict 只能由 typed authority 构造并追加 |
| I5 | 投影可重建 | 删除所有派生文件后由同一事件前缀重建 |
| I6 | 控制律不含认知 | stop law 只读 deadline、预算、lease、cancel、verdict、unsafe unknown |
| I7 | 适配器不拥有真相 | adapter 返回 observation/result，不追加 Run 状态 |
| I8 | 失败关闭 | 账本、schema、lease、能力或证据不明确时停止危险动作 |
| I9 | Agent 不能写权威面 | 容器只写 scratch，宿主只经应用层编码 |
| I10 | benchmark 不污染内核 | kernel/evidence/projection 不导入 TSec、Pi、Docker、FastAPI |
| I11 | 预算先预留后结算 | reservation 在 dispatch 前提交，settlement 可幂等重放 |
| I12 | Context 有界且稳定 | 相同事件前缀和配置生成相同上下文 |

## 4. 方案选择

### 方案 A：只整理现有模块

按职责合并大文件、减少重复 helper，但保留现有多个权威文件。

- 优点：风险最低，短期改动小。
- 缺点：没有解决真相分散、动作身份晚、预算事后记账，属于结构美化。

### 方案 B：继续横向补齐因果内核

按现有 Stage 2 计划先为所有路径增加 ActionAuthority，再逐步迁移投影。

- 优点：每一步改动较窄，当前行为回归风险较低。
- 缺点：双权威、双写和兼容层会长期存在；在所有适配器同时接入前无法证明系统真的简化。

### 方案 C：纵向单权威切片（选中）

选择一个完整 Run 路径，一次性打通“账本写入 -> 投影读取 -> 动作接纳 -> 恢复 -> 终态”，
其他路径暂时保持 v2；切片验收后删除该路径的旧权威分支，再复制到 benchmark 路径。

- 优点：最快获得“单权威真正工作”的端到端证据；每个阶段都有可删除的旧实现。
- 缺点：迁移期同时存在 kernel authoritative 与 legacy 路径，必须用明确开关隔离，禁止同一 Run
  混用。

选择 C。理由是架构风险只能通过一个真实闭环被证伪；横向补齐会把“双写是否正确”变成长期
测试负担，而不是迁移手段。

## 5. 系统模型

### 5.1 数据分类

```text
run_dir/
  ledger.db                   权威：唯一追加式因果事件流
  artifacts/                  权威：不可变、内容寻址的证据/输出工件
  agent-workspace/            不可信：Agent 可写 scratch
  world.context.txt           投影：Agent 读模型
  world.snapshot.json         投影：调试/兼容快照
  trace.jsonl                 投影：操作者审计导出
  progress.json               投影：预算与统计快照
  checkpoint.json             投影/兼容提示：恢复游标
  result.json                 投影：终态读取模型
```

除 `ledger.db` 与 `artifacts/` 外，其他运行文件都不是权威事实。`artifacts/` 不是第二状态库，
只保存被事件引用的内容寻址 payload；artifact 缺失或 hash 不匹配会使对应 Evidence 不可用，
而不是触发状态猜测。任何投影落后都从账本重建，不能用投影修正账本。

### 5.2 七个原语

| 原语 | 含义 | 生产者 |
| --- | --- | --- |
| Run | 一个目标、一组预算、唯一终态 | 应用层 |
| Attempt | 一次 lease、隔离环境与会话 epoch | 应用层 |
| Action | 一次被授权或有 lease 的执行意图 | 应用层/kernel authority |
| Observation | 授权观察者实际捕获的原始事实 | Harness probe、verifier、evaluator |
| Claim | Agent 或辅助代码的断言，不是证据 | Agent、模型、工具输出 |
| Evidence | Observation 与命题的不可变绑定引用 | 授权 verifier/evaluator |
| Verdict | 基于 Evidence 的 predicate/objective 结论 | 授权 verifier/evaluator |

模型输出、工具退出码、submission accepted 和人工 hint 都不得跨层升级为 Evidence 或 Verdict。

## 6. 组件与模块

### 6.1 依赖图

```text
entrypoints (CLI, Gateway)
   |-------------------------> adapters (Pi, model, benchmark, verifier, artifacts)
   |                                      |
   v                                      | implements
runtime (RunService, AttemptScheduler) -> ports
   |          |             |
   |          |             +-> control (stop law, budget)
   |          +---------------> evidence (typed capability)
   v
kernel (events, store, authority, recovery)
   |
   v
projections (world, context, run view, trace, audit)
```

依赖方向固定为：entrypoints -> runtime -> kernel/control/evidence/ports；
entrypoints -> adapters -> ports。adapters 只能导入 `harness.runtime.ports` 中的纯协议，
不得导入 RunService、kernel 或 projections；projections 只读 kernel 事件和 artifacts。

### 6.2 模块职责

| 组件 | 模块（目标路径） | 唯一职责 | 不得承担 |
| --- | --- | --- | --- |
| 入口 | `harness.cli` | 参数/任务/agent 解析、依赖组装、退出码 | Run 状态机、证据判断 |
| 入口 | `harness.gateway` | 模型/Gateway 工具的认证、脱敏与请求翻译 | 绕过 authority 直接调用 provider |
| 应用 | `harness.runtime.run_service` | Run/Attempt 生命周期、loop 驱动、终态收敛 | 外部 effect 实现、投影算法 |
| 应用 | `harness.runtime.attempt_scheduler` | 并行 attempt、lease、预算份额、取消 | 目标 verdict、直接写账本 |
| 应用 | `harness.runtime.ports` | executor/provisioner/evaluator/clock/artifact 协议 | 任何具体基础设施导入 |
| 内核 | `harness.kernel.events` | 事件 envelope、payload schema、事件家族 | I/O、进程管理 |
| 内核 | `harness.kernel.store` | 单写入者、顺序、事务、哈希链、replay | 业务决策、投影渲染 |
| 内核 | `harness.kernel.authority` | action admission、lease fence、预算 reservation/settle | 调用 provider/verifier |
| 内核 | `harness.kernel.recovery` | 启动 replay、未知动作识别、异常报告 | 自动重试、修改既有事件 |
| 控制 | `harness.solver_policy` | 纯 stop law | world 查询、模型/工具调用 |
| 控制 | `harness.budget` | 预算计算、份额、held/settled 算术 | 持久化、dispatch |
| 证据 | `harness.verification` | typed evidence/verdict 构造与授权 registry | 决定 Run 终态 |
| 投影 | `harness.world` | 由 claim/observation/evidence 派生 WorldView | 反向写权威状态 |
| 投影 | `harness.world.context` | 有界、确定性的 Agent 上下文 | 推荐下一步、解析 knowledge |
| 投影 | `harness.progress` + `harness.runtime.results` | Progress、Result、metrics、checkpoint 视图 | 独立维护预算/终态真相 |
| 投影 | `harness.trace` | 从账本导出审计 trace；兼容期可先写导出 | 先于账本写权威事件 |
| 投影 | `harness.audit` | 账本完整性、证据闭包、投影一致性检查 | 修复或覆盖账本 |
| 适配器 | `harness.pi_adapter` + `harness.pi_container` | Pi RPC、容器、挂载、事件翻译 | 解释成功、签发 evidence |
| 适配器 | `harness.gateway_runtime` | provider transport 与 run-bound 网关组合 | 独立决定预算或 Run 状态 |
| 适配器 | `harness.benchmark.*` | TSec provision/submit/evaluate 翻译 | 把 accepted 当 objective verified |
| 适配器 | `harness.verifier` | bubblewrap verifier 执行与 raw observation | 自行宣布 objective verdict |
| 适配器 | artifact store | 内容寻址写入、hash 校验、脱敏 | 可写业务状态 |

以上是目标物理形状。迁移期可以保留旧 import path 作为薄兼容包装，但包装层不得拥有第二份状态。

### 6.3 现有模块处置

| 当前模块 | 目标归属 | 处置 |
| --- | --- | --- |
| `harness.ledger` | kernel events/store | 先保留兼容导出，切片 2 后删除具体 store 类型依赖 |
| `harness.trace` | projection trace | 翻转写入顺序：账本先提交，trace 导出可失败重建 |
| `harness.world.sqlite` | world view cache/importer | 失去权威写权限；只作缓存、兼容导入和性能层 |
| `harness.progress`、`harness.runtime.results`、`harness.runtime.checkpoint` | run view projection | 只从事件派生，不再拥有独立预算/恢复真相 |
| `harness.orchestrator` + `harness.benchmark.runner` 生命周期部分 | RunService/AttemptScheduler | 保留 adapter 翻译，删除重复生命周期 |
| `harness.action_verifier` | action dimensions + evidence registry | 退出控制脊，不再产生通用 ActionVerdict |
| `harness.solver_policy`、`harness.budget` | control | 保持纯函数，只接收显式输入 |
| `harness.pi_*`、`harness.gateway*`、`harness.benchmark.*`、`harness.verifier` | adapters | 保留实现，禁止写权威状态或签发 Verdict |

### 6.4 目标目录形状

```text
src/harness/
  cli.py
  gateway.py
  gateway_runtime.py
  runtime/
    run_service.py
    attempt_scheduler.py
    ports.py
    results.py
  kernel/
    events.py
    store.py
    authority.py
    recovery.py
  solver_policy.py
  budget.py
  verification.py
  world/
    __init__.py
    view.py
    context.py
    retrieval.py
  progress.py
  trace.py
  audit.py
  pi_adapter.py
  pi_container.py
  benchmark/
    protocol.py
    runner.py
    tsec_adapter.py
    tsec_client.py
  verifier.py
  artifacts.py
  agent_workspace.py
```

不要求一次性搬动文件。首先通过架构测试固定依赖方向，再在纵向切片中逐项迁移和删除。

## 7. 写权限矩阵

| 调用者 | 可写内容 | 禁止 |
| --- | --- | --- |
| kernel.store | 所有已注册事件家族 | 解释业务、调用 adapter |
| kernel.authority | action/lease/budget 事件批量 | 调用外部 effect |
| runtime.RunService | 经 typed kernel API 追加 Run/Attempt/Claim/Observation 事件 | 直接打开 SQLite、写投影作为真值 |
| verification registry | Evidence/Verdict 事件的授权值 | 写 Run 终态、绕过 schema |
| projections | 派生文件与投影游标 | 追加账本、修复权威状态 |
| adapters | 外部系统与 artifacts | 写 ledger、world、result、evidence capability |
| Agent | `agent-workspace/` 与容器内 scratch | 写 run root、账本、结果、证据、凭据 |
| operator | hint/cancel/reconcile 事件 | objective verdict、伪造 observation |

`LedgerAppender` 保留为 kernel 内部事务端口。运行时组件不能直接持有裸 appender；它们通过
typed `RunKernel` API 写入。这样 producer 字段只是来源元数据，不是授权凭证。

## 8. 内核契约

### 8.1 事件 envelope

```text
ledger_seq, event_id, run_id, attempt_id?, producer, trust_tier,
kind, schema_version, occurred_at, causation_id?, correlation_id?,
payload, artifact_refs[], prev_hash, record_hash
```

- `event_id` 全局唯一，重复 ID 只能对应完全相同的 content。
- `ledger_seq` 在 Run 内单调递增；reopen 继续序列和哈希链。
- 事件不可更新或删除；纠正通过追加 supersede/contradict/reconcile。
- 批量 admission + reservation 必须在同一事务提交。
- 哈希链只检测事故性损坏和投影漂移，不抵抗宿主管理员。

### 8.2 事件家族

| 家族 | 事件 | 关键约束 |
| --- | --- | --- |
| Run | admitted、phase、terminal | terminal 后不再追加执行事件 |
| Attempt | planned、started、draining、finished、failed | lease epoch 单调 |
| Authority | lease issued/revoked、action admitted/denied | admission 先于 dispatch |
| Execution | dispatched、returned、failed | returned/failed 不等于效果已确认 |
| Observation | captured、artifact committed | 必须绑定 producer 与 artifact hash |
| Accounting | budget reserved/settled/released | settle 幂等，冲突值 fail closed |
| Claim | claimed、superseded | 永不升级为 Evidence |
| Evidence | recorded、predicate verified/contradicted | 必须引用 Observation 与 artifact |
| Objective | submission proposed/accepted/rejected、verdict | accepted 不等于 objective verified |
| Operator | hint、cancel、reconcile、resume | 不改变历史，只追加意图 |

### 8.3 多维动作状态

不再使用单一 `ActionVerdict` 表达所有问题。每个 Action 至少有五个正交维度：

```text
admission   = admitted | denied
dispatch    = not_dispatched | dispatched
execution   = pending | returned | failed | unknown
effect      = unobserved | observed | contradicted | unknown
settlement  = held | settled | released
```

约束：

- `execution=failed` 不推出 `effect=not_observed`；
- `returned` 不推出成功，只是 adapter 返回；
- `effect=observed/contradicted` 必须有授权 Observation；
- `unknown` 只能通过显式 reconcile 变更；
- objective success 是独立 Verdict，不属于 action effect。

未知动作按风险分为三类，severity 由 effect_class 和 `side_effect` 派生，不能由 Agent 自报：

| 严重度 | 触发条件 | 允许行为 | Objective 影响 |
| --- | --- | --- | --- |
| `retryable_observation` | `observation_effect`，只读 | 可显式重试，记录 `retry_of` | 无 |
| `cost_uncertain` | `model_effect` 未完成 settle | 阻塞同一 attempt/budget scope 的新模型 dispatch，直到 reconcile | 不阻塞独立 Evidence；result 标记 accounting incomplete |
| `unsafe_side_effect` | `brokered_effect` 或带副作用的 `lease_effect` | 阻塞同 idempotency scope 的新 dispatch，禁止自动重试 | reconcile 前不得产生 verified objective |

`unsafe_side_effect` 不阻止 Run 到期关闭，但 Run 必须显式携带 unknown action IDs，且不得对外
表现为 objective verified。不同 Attempt 可以继续工作；共享的 brokered scope 必须全局阻塞。

### 8.4 效果分类与接纳

| effect_class | 例子 | 授权点 | 恢复语义 |
| --- | --- | --- | --- |
| `model_effect` | 模型请求、计费 | Gateway admission + reservation | 可重试但必须同预算语义，未知时人工/观测对账 |
| `brokered_effect` | submission、外部 API、宿主命令 | 逐动作 admission | dispatch 后未知不得自动重试 |
| `observation_effect` | verifier、只读 probe | capability + admission | 只读可安全重放，但仍保留不同 action ID 或显式 retry 原因 |
| `lease_effect` | 容器内 shell/文件/网络工具 | Attempt lease 的挂载、能力、网络 profile | 观测后补 identity；预算允许一次在途越界，不宣称逐命令预批 |

接纳规则：

1. 确定性 `action_id` 由 run、attempt、effect_class、scope、idempotency_key 计算；
2. payload hash 与相同 action_id 不一致时抛出 `ActionConflict`；
3. authoritative 模式先提交 `admitted + reserved`，成功后才返回 dispatch handle；
4. 预算不足、ledger 不可写、lease 过期或 schema 不明确时，危险动作零副作用并失败关闭；
5. shadow 模式只记录“本应 deny/admit”，不改变 v2 行为；
6. lease_effect 不伪造逐命令前置授权，容器 lease 是授权边界。

### 8.5 预算状态机

```text
reserved -> settled(actual)
reserved -> released(failed before dispatch)
reserved -> held/unknown -> settled | released after reconcile
```

- 预算判断使用 `settled + held + requested <= limit`。
- 重复 settle 相同数值是 no-op；不同数值是冲突。
- 实际结算可以超过 reservation，但必须标记 `over_reservation=true`。
- unknown 保持 held，不自动 release。
- lease_effect 在观测后结算；允许单个在途动作越过软预算，之后由 stop law 停止新 dispatch。

## 9. 应用层契约

### 9.1 RunService

单一生命周期服务，负责：

1. 创建/恢复 Run；
2. 调用 kernel 进入 admitted/provisioning/running 阶段；
3. 编排 AttemptScheduler 与 solver loop；
4. 把 adapter 返回的 claim/observation 经 typed API 记录；
5. 处理 submission、verification、terminal closure 与 teardown；
6. 触发投影更新和 audit。

RunService 不实现 Pi、Docker、TSec、HTTP 或 bubblewrap。

### 9.2 AttemptScheduler

负责：

- 为同一 Run 规划最多 N 个 Attempt；
- 分配预算份额；
- 签发/撤销 lease 与 fencing epoch；
- 处理并行、取消、preemption 和单 attempt 失败隔离；
- 保证任一 attempt 的 observation 不能绑定到另一个 attempt 的 action。

任何 attempt 失败都不会直接覆盖 Run 终态；Run 终态只由 RunService 的收敛规则产生。

### 9.3 Solver loop

Solver loop 是应用层状态驱动循环，只做：

```text
render context -> poll agent events -> encode claims -> dispatch actions
              -> collect observations -> evaluate stop law -> repeat
```

它不解析 knowledge、不推荐动作、不根据“无进展”steer，也不自行宣布成功。

### 9.4 终态收敛

同一 Run 的终态只能由第一条 terminal 事件决定，后续 terminal 冲突必须失败关闭。收敛优先级：

1. 授权 objective Verdict 为 verified，且不存在 `unsafe_side_effect` unknown：`succeeded`；
2. operator cancel 已被接受：`cancelled`；
3. 未 verified 且 deadline 到达：`exhausted`；
4. 未 verified 且预算耗尽：`exhausted`；
5. 未 verified 且 Harness/adapter 发生不可恢复错误：`failed`；
6. 所有 Attempt 正常结束但没有 verified objective：`failed`，公共结果仍映射为 `finished`。

公共 `result.status` 继续兼容 `objective_completed | timeout | budget_exceeded | finished`，
本地取消/错误仍使用既有 `cancelled | error` 路径。`objective_status`、`unknown_actions` 和
`accounting_incomplete` 是附加字段，不改写旧字段含义。

任意 Attempt 的 verified objective 都可以结束整个 Run；其他 Attempt 的失败不得覆盖该成功。
`unsafe_side_effect` 存在时，即使出现 raw verified verdict，也只能作为 provisional verdict，
不得进入优先级 1。

## 10. 证据与信任契约

- `Observation` 必须包含 producer capability、capture time、run/action correlation、artifact hash。
- `Evidence` 是不可变引用，绑定 predicate、observation ID、artifact hash 和 supersession。
- `Verdict` 只引用 Evidence；objective verdict 与 Run terminal result 分开。
- Agent、模型、工具原始输出只能生成 Claim 或 untrusted artifact。
- submission accepted 是 submission observation，不是 objective verdict。
- VerifierRegistry 只能从组合根接收授权 verifier/evaluator；默认空 registry 对所有目标失败关闭。
- 外部 verifier adapter 返回 raw observation，registry 决定是否构成 Evidence。
- 不存在“最新 observation 自动验证任意 action”的逻辑；必须显式 correlation。

## 11. 投影契约

每个投影都必须声明：

```text
name, projection_version, input_seq, config_hash, content_hash
```

| 投影 | 输入 | 输出 | 重建规则 |
| --- | --- | --- | --- |
| WorldView | claim/observation/evidence/verdict | facts、claims、unknowns、failures | 纯 reducer + artifact 引用 |
| Context | WorldView + budget/progress + config | 有界文本 + as_of_seq | 相同前缀和配置字节稳定 |
| Progress | budget/action/observation/claim | counts、usage、steer metrics | 只聚合，不持有独立预算 |
| Result | run/objective/submission/verdict | 终态读取模型 | terminal 事件为唯一来源 |
| Checkpoint | ledger tip + resume hints | 兼容 checkpoint | 落后时忽略并从账本生成 |
| Trace | 全事件流 | `trace.jsonl` | 只追加导出，可全量重放 |
| Audit | ledger + artifacts + projections | issue list | 只报告，不修复 |

WorldView 必须区分 `verified facts`、`evidence observations`、`claims`、`unknowns` 和
`failures`，不得把 Agent 假设渲染成已确立事实。Context 不包含下一步建议或候选动作。
wall clock 只作为显式 ephemeral 输入；相同 `as_of_seq`、配置和显式时钟快照必须生成稳定文本。

## 12. 适配器契约

所有适配器遵循同一原则：**执行 effect，返回 observation；不解释 Run 真相。**

| 适配器 | 输入 | 输出 | 禁止 |
| --- | --- | --- | --- |
| Pi executor | agent spec、lease、workspace | untrusted agent events、tool observations | 写 ledger/world/result |
| Model Gateway | admitted model action + credentials | model output、usage observation | 自行 admit/settle、泄露 key |
| Benchmark | provision/submit/evaluate request | endpoint、submission/evaluator observation | 决定 objective verdict |
| Task verifier | task、candidate、capability | raw verifier observation | 自行签发 Evidence/Verdict |
| Artifact store | bytes + media metadata | artifact ref/hash | 接受未脱敏秘密 |

Adapter 可以重试**只读**动作，但必须由应用层显式产生新的 action 或带相同幂等键的重试记录。
写副作用 adapter 不得自行重试。

## 13. 恢复协议

启动顺序固定：

1. 打开 ledger，校验 schema、序列和哈希链；
2. replay 得到 Run/Attempt/Action/Budget 状态；
3. 将 `dispatched` 且无 execution/effect 终态的动作追加为 `unknown`；
4. 保持相关 reservation 为 held；
5. 询问 adapter 是否可安全探测；只追加 observation，不改写旧事件；
6. 从账本 tip 重建缺失/过期投影；
7. 只有恢复状态明确且 lease 仍有效时，RunService 才继续或重新 dispatch。

禁止：

- 用 checkpoint 覆盖账本；
- 把 unknown 自动变成失败并释放预算；
- 用新的 action_id 静默重试旧的写副作用；
- 在 ledger 不可读时继续运行危险动作；
- 在 terminal 后追加执行事件。

危险 unknown 可以允许 Run 到期关闭，但 Run 必须显式列出 unknown IDs，且不得产生 verified
objective。

## 14. 安全边界

- Agent 容器只挂载只读 task/knowledge 和可写 scratch；不能挂载 run root、ledger、artifacts 权威面、
  result、checkpoint、verifier capability 或宿主凭据。
- 宿主 Gateway 是模型凭据的唯一持有者；run-bound Gateway 必须复用同一个 ActionAuthority。
- 独立 gateway/subagent 不构成 Run，不得签发 kernel Evidence 或 ObjectiveVerdict。
- Artifact 写入前统一脱敏和大小限制；账本只存引用与 hash，不存秘密或无限原始输出。
- producer/trust_tier 是审计元数据，授权由 typed capability 和调用路径决定。

## 15. 迁移切片与删除条件

### 15.1 迁移期兼容面

| 表面 | 迁移期承诺 | 删除切片 |
| --- | --- | --- |
| `result.json` 既有字段 | 保持字段和 status 语义，新增字段只做版本化扩展 | 不删除 |
| `trace.jsonl` 事件 shape | 保持可导入；写入顺序改为 ledger 先、trace 后 | 不删除 |
| CLI 命令/flag/退出码 | 保持现有契约；新增 kernel mode 用显式 flag/env | 不删除 |
| Python import path | 现有公共导入保留薄包装 | 切片 3 后按版本迁移 |
| `world.db`/`world.*.json` | 继续可读，但降为可重建缓存/投影 | 切片 3 后可替换存储 |
| `progress.json`/`checkpoint.json` | 继续生成兼容视图，但不作为恢复真相 | 切片 3 后可省略 |
| `harness gateway` 独立模式、`harness subagent` | 行为保留，但不承诺 Run 级因果/预算语义 | 不纳入 Run kernel |
| ledger store/event schema | 通过 payload 版本扩展，不原地重解释旧字段 | 需要版本化迁移 |

### 15.2 切片 1：local Run 单权威闭环

选择 `harness run` 一条完整路径，建立 ledger authoritative + run-bound Gateway + task verifier +
世界/上下文/结果投影。验收后再删除该路径的旧权威分支。

local authoritative 路径必须经过 run-bound Gateway；direct-provider Pi 继续作为 legacy，
不得声称拥有完整因果和预算预留。

必须证明：

- 删除全部投影后可以从 ledger 重建；
- 模型调用在 dispatch 前被接纳；
- verifier 在无 Evidence 时不产生 verified；
- dispatch 后杀进程，重启得到 unknown 而非重放；
- 同一事件前缀产生稳定 Context/Result；
- audit 能发现缺失 artifact、跨 run evidence 和投影漂移。

### 15.3 切片 2：benchmark 单权威闭环

把同一 RunService/AttemptScheduler 接到 benchmark adapter；加入 submission 幂等和 evaluator
observation。验证多 attempt 共享提交去重与终态收敛。

### 15.4 切片 3：删除双写与旧权威

删除条件全部满足才允许：

- local 与 benchmark 都只从 ledger 产生权威投影；
- shadow/authoritative 不再需要双写；
- 五个崩溃窗口都有故障注入证据；
- 真实 sweep 满足下列发布门；旧 import path 有版本化兼容包装或迁移说明。

真实 sweep 使用 BEN-07 的 10 个未解/未跑挑战（`b-03`、`c-02`..`c-06`、`a-03`、`a-05`、
`e2-04`、`e3-04`）加 4 个已解控制，配置与 2026-10-16 基线一致：

- solved controls 成功率 100%，总 submission rate 不低于基线；
- median time-to-first-submission 不高于基线 110%；
- provider/evaluator 报告 usage 的调用，settled usage 与报告值无未解释差异；
- 正常 sweep 的 `unsafe_side_effect` unknown 为 0，重复 submit key 为 0，自动重放为 0；
- 五个崩溃窗口的故障注入全部通过，恢复后未知动作只对账、不重放；
- 删除所有投影后可 100% 重建，golden Context/Result 字节稳定。

删除项：v2 权威 World 写路径、独立 Progress/Checkpoint 真相、Trace 先写后补账本的顺序、
通用 ActionVerdict 热路径、direct-provider “完整因果”声明、永久 shadow 模式。

## 16. 验收测试

| 文件 | 必测行为 |
| --- | --- |
| tests/test_kernel_store.py | 原子批量、重复 ID、冲突、哈希链、reopen |
| tests/test_kernel_authority.py | admission/deny、reservation、幂等 settle、unknown、lease fence |
| tests/test_kernel_recovery.py | 五个崩溃窗口不重放、not settled 不重复计费 |
| tests/test_trust_boundary.py | 伪造 producer、跨 run evidence、Agent 文本/exit code 不能 verified |
| tests/test_projections.py | 删除后重建、golden world/context/result/trace、字节稳定 |
| tests/test_run_service.py | 单一生命周期、attempt 隔离、terminal 收敛、teardown 幂等 |
| tests/test_vertical_slice_e2e.py | fake Pi/model/verifier 的完整 Run、崩溃恢复、投影重建 |
| tests/test_architecture.py | 依赖方向、control spine 冻结、retired subsystem 不复活 |
| tests/test_control_metrics.py | 模块/LOC/单文件上限及实测值 |
| tests/test_docs_consistency.py | 文档路径、CLI、环境变量与新模块一致 |

当前主机上 10k 事件的 provisional 性能门：

- batch append <= 5,000 ms（基线为逐事件 13,471 ms）；
- ledger verify <= 1,000 ms（基线 196 ms）；
- full projection <= 1,000 ms（基线 266 ms）；
- ledger <= 10 MiB（基线 4.3 MiB）。

实现计划可以基于新的实测结果收紧这些上限，但不能无证据放宽。没有负面测试、故障注入或真实
端到端证据的模块不计完成。

## 17. 复杂度与简化预算

每个新增模块必须满足至少一项：

1. 独占一个不可替代的状态机；
2. 独占一个安全/信任边界；
3. 是一个可替换的 projection；
4. 是一个外部 effect adapter；
5. 是组合根，且不包含业务判断。

否则不新增模块，优先删除或组合已有代码。

工程护栏：

- 不提高当前 `MAX_MODULES=60` 和 `MAX_SRC_LINES=16_983`；任何新增模块必须伴随等量或更多旧
  模块删除，切片完成后重新实测并下调；
- 任何单文件超过 700 行必须证明其内部不可分割，并登记说明；
- 兼容包装必须标注删除切片，不允许永久存在；
- 新增抽象如果不能让旧分支删除，就回滚；
- projection 不得反向调用 application/kernel write API。

## 18. 我做的设计决定

审批时可否决以下决定：

1. 采用方案 C：local Run 纵向单权威先行，benchmark 第二。
2. 目标逻辑层为 entrypoints / runtime / kernel / control / evidence / projections / adapters。
3. `ledger.db` 是唯一权威；`artifacts/` 是唯一不可变工件权威；其他运行文件为投影。
4. `LedgerAppender` 收归 kernel 内部，应用层只使用 typed `RunKernel` API。
5. Action 使用 admission/dispatch/execution/effect/settlement 多维状态，不再用单一 verdict。
6. model/brokered/verifier 动作前置接纳与预算预留；lease_effect 只承诺容器 lease 授权和观测后
   identity。
7. kernel 不导入 FastAPI、Docker、subprocess、httpx、TSec 或 benchmark adapter。
8. adapters 返回 observation/result，不写 ledger、不签发 Verdict。
9. Context 是确定性投影，不包含下一步建议；knowledge 仍由 Agent 自主读取。
10. 迁移开关只允许按 Run 选择 shadow/authoritative，禁止同一 Run 混用两个写入者。
11. unknown 分 `retryable_observation`、`cost_uncertain`、`unsafe_side_effect` 三档，只有
    第三档阻止 verified objective。
12. 终态优先级固定为 verified objective > cancel > timeout > budget > fatal error > finished。
13. 迁移期承诺的公共面为 result/trace、CLI、现有 import path；旧 world/progress/checkpoint
    文件降为可读投影。
14. 真实 sweep 使用 BEN-07 的 10+4 挑战集，并采用本节冻结的提交率、时延、预算、unknown 和
    恢复门槛。

## 19. 留给实现计划决定的事项

1. `KernelEvent` 各 payload 的精确 schema、默认值和迁移版本号；
2. `RunKernel` typed API 的最小方法集合与错误分类；
3. local Run 的 run-bound Gateway 是否与 RunService 同进程，以及凭据传递细节；
4. attempt lease、fencing token 和容器生命周期的具体绑定方式；
5. artifact store 的 hash 路径、保留、清理和脱敏格式；
6. golden ledger fixture 的具体历史 Run 与投影比较算法；
7. 10k 事件 replay、context 重建和 projection 更新的性能上限；
8. 兼容包装的版本边界与删除方式；
9. 单文件 700 行规则是否需要针对现有 adapter 设例外；
10. Docker 崩溃矩阵的具体注入点与 CI/本地分工。

## 20. 审批门

本文获批后，下一步才是实现计划。实现计划必须按切片拆分，每片先写失败/负面测试，再迁移写入或
读取路径；任何阶段如果无法删除旧分支、无法证明投影等价或无法处理 unknown，就必须停止并
回滚，而不是把影子账本永久留在生产路径中。
