# 第一性原理架构 v3：因果与信任内核

> 状态：目标设计，尚未实现。当前生产架构仍是
> [agent-architecture-v2.md](../../agent-architecture-v2.md)；在迁移验收完成前，v2 的
> 信任边界、事件格式和测试约束继续有效。

## 1. 一句话

**一次 Run 只有一条因果账本；Agent 的所有输入输出都经边界变成记录；World、Context、
Trace、Result、Progress 和 Checkpoint 都是账本的可重建投影。**

v2 已经确立了正确的认知边界：模型负责推理，Harness 负责环境、信任、记账与恢复。v3 不把
认知拿回 Harness，而是补齐 v2 尚未从第一性原理解决的另一半：

> **Harness 不只拥有“状态”，还必须拥有“因果”。**

没有因果身份的状态无法回答：这次副作用是谁授权的、是否真的发生、失败后能不能重试、哪条
证据支持了哪个结论。v3 的目标不是增加更多状态字段，而是让这些问题在结构上只有一个答案。

## 2. 第一性原理

### 2.1 不可再简化的前提

1. **Agent 不可信且有错。** 它会误报、伪造、跳过步骤、重复调用和误判自己的成功。
2. **环境不可完全观测。** Harness 只能依赖授权观察者实际捕获的观测，不能依赖“应该发生”。
3. **副作用不可逆且会超时。** 一个请求发出后，进程崩溃不代表副作用没有发生。
4. **真相有生产者。** Agent 的文本、工具退出码、平台裁决和独立探针不是同一种事实。
5. **可变缓存一定会漂移。** 任何可修改的宿主文件都可能在崩溃、并发或手工操作后偏离真相。
6. **模型负责探索，确定性代码负责边界。** Harness 不应替 Agent 规划，但必须替系统裁决权限、
   预算、因果和终止。

### 2.2 由前提推导出的职责

Harness 必须且只必须承担六件事：

| 职责 | 要回答的问题 | 不负责的事 |
| --- | --- | --- |
| 授权 | 谁可以在什么范围内做什么 | 应该先做哪一步 |
| 记录因果 | 一次动作在何处被接纳、发出、观测和对账 | 如何找到漏洞 |
| 隔离执行 | Agent 能在哪个容器、网络和挂载中运行 | 替 Agent 选择工具 |
| 记账 | 被预留和实际消耗了多少资源 | 根据行为猜测是否“没有进展” |
| 验证 | 什么证据足以支持哪个结论 | 把模型自述当证据 |
| 恢复与终止 | 崩溃后哪些动作可以重放，何时必须停下 | 在未知副作用上盲目重试 |

这六件事之外的能力默认不进入控制脊（control spine）。知识目录、提示词、工具选择、子目标、
假设和探索顺序都属于 Agent。

## 3. 对 v2 的结构性诊断

v2 的边界和信任模型是可靠的，但当前实现仍有五个结构性问题。它们不是靠继续增加字段就能消失。

### 3.1 真相被分散在多个文件中

World 事件、trace、progress、checkpoint 和 result 分散在不同文件中，审计命令只能在事后拼合
一致性。一次崩溃可能同时留下“World 已更新、checkpoint 未更新”和“result 已写入、progress
尚未投影”的组合。只要这些文件都被当作权威，系统就必须持续处理它们的交叉不一致。

### 3.2 动作身份产生得太晚

当前容器内的工具调用主要是在 Pi 事件到达后才被归一化为 action。对于容器外副作用（提交、
验证、环境操作），部分路径有明确生命周期；但对于整个系统的因果模型，动作没有统一的接纳点，
也没有统一的 `effect_class`、`idempotency_key` 和外部对账句柄。

结果是可以记录“某次工具调用返回了”，却不能稳定回答“这次副作用是否已经越过边界”。

### 3.3 把执行状态、效果状态和目标状态混在一起

执行失败、效果未观测、效果矛盾和目标未完成是四个不同问题。把它们压缩成一个 action verdict，
会自然产生 `pending`、`unavailable` 和大量无信息 steer。2026-10-16 sweep 中的 656 条 pending
裁决就是这种模型错位的证据，而不是一个需要继续调参的分类器。

### 3.4 Agent 主张与已确立事实共享同一个可写面

当前 reducer 会把 Agent 写入对象降为 `claim`，这是正确的；但 claim 和 evidence 仍共享一个
World 命名空间，Agent 的假设、失败和工具流水会与权威事实竞争上下文预算。结构上更安全的做法是：
Agent 的 scratch 与系统证据分开存放，World 只是证据账本的只读投影。

### 3.5 预算和恢复仍是“事后记账”

当前 budget 能准确统计已用资源，但一个正在执行的模型调用或工具调用可以在超限前完成。恢复则
依赖 checkpoint 与 progress 两套游标。第一性原理下的预算应是**先预留、后结算**；恢复游标应
从账本本身推导，而不是再维护一份可漂移的控制文件。

## 4. v3 不变量

以下不变量优先于模块划分和 API 形状。任何实现如果违反其中一条，就不属于 v3。

### I1. 单一权威

每个 Run 只有一条追加式因果账本可以定义已发生的事实。其他持久文件都是投影或导入缓存。

### I2. 因果先行

跨宿主边界的动作必须先被接纳并获得稳定的 `action_id`，再允许 dispatch。容器租约内部的
命令可以依靠容器边界授权，但一旦被观测，也必须归入同一个 action 因果链。

### I3. 未知不重放

当外部副作用处于未知状态时，系统不得自动重试。未知只能通过适配器查询、独立观测或操作者
对账转为已知；否则 Run 必须停留在可审计的非成功状态。

### I4. 信任不升级

Agent 输入、模型输出、工具原始输出和 benchmark 任务内容永远不能直接写入 `evidence` 或
`verified`。只有被组合根显式授予 producer capability 的观察者或验证器可以签发这些记录。

### I5. 状态可重建

相同账本前缀、相同投影版本和相同投影配置必须生成相同的 World、Context、Trace、Result 和
审计视图。

### I6. 控制律无认知

控制律只读取外部可判定条件：deadline、预算、lease 有效性、操作者取消、目标裁决和不安全
未知动作。它不从重复、无进展、消息长度或模型行为中推断“应该换一种打法”。

### I7. 适配器不拥有真相

Pi、Docker、TSec 和 verifier 适配器只做翻译和资源操作。它们不能决定 Run 成功，不能直接
修改账本，也不能把 benchmark 特例带进内核。

### I8. 失败关闭

账本损坏、事件 schema 不匹配、证据引用缺失、租约冲突或适配器无法确认结果时，系统必须暴露
明确的未知/错误，不得猜测一个成功状态。

## 5. 核心模型

### 5.1 七个原语

| 原语 | 定义 | 权威归属 |
| --- | --- | --- |
| Run | 一个目标、一个 evaluator、一组总预算和唯一终态 | Harness |
| Attempt | 一份隔离环境 lease 与一个 Agent 会话 epoch | Harness |
| Action | 一次被接纳的边界副作用或容器内执行 | Harness 记录，Agent 发起 |
| Observation | 授权观察者实际捕获的原始事实 | Harness/verifier/evaluator |
| Claim | 关于状态、结果或因果的断言，不具证据权威 | Agent 或辅助代码 |
| Evidence | 将 Observation 绑定到某个命题或动作的不可变引用 | 授权验证器 |
| Verdict | 基于 Evidence 对目标或明确谓词作出的授权结论 | 授权 verifier/evaluator |

### 5.2 Run、Attempt 和 Action 的关系

```text
Run
├── objective + evaluator + run budget + terminal record
├── Attempt A ── lease A ── Pi session A ── scratch A
├── Attempt B ── lease B ── Pi session B ── scratch B
└── run-level commitment channel (submission dedupe + evaluator)

Action belongs to exactly one Run and, when executed inside a session, one Attempt.
```

当前实现已经有同题多 executor 的竞争语义，v3 把它提升为显式模型：Run 拥有最终目标，Attempt
拥有隔离环境、会话和预算份额。这样单个 Attempt 崩溃不会改变 Run 的目标，也不会让多个会话
各自成为“半个 Run”。

### 5.3 Action 的分类

Action 不只有一种授权方式：

| 类别 | 例子 | 授权点 | 可恢复性 |
| --- | --- | --- | --- |
| `lease_effect` | 容器内 shell、文件工具、目标网络请求 | 容器 lease 的挂载、能力、网络 profile | 由 effect class 和适配器对账决定 |
| `brokered_effect` | benchmark 提交、宿主 verifier、外部 API | Action Broker 的逐动作接纳 | 必须使用幂等键或保持 unknown |
| `model_effect` | 模型请求、模型计费 | Gateway 的预算预留和凭据边界 | 可重试但必须计入同一预算 |
| `observation_effect` | 只读探针、渲染、evaluator 查询 | 观察者 capability | 只读动作可安全重放，仍要保留因果 ID |

这解决了一个重要现实：Pi 内置工具无法被 Harness 对每条 shell 命令做前置审批。v3 不假装能够
做到，而是把容器 lease 作为这类动作的授权边界；只有跨宿主或高价值副作用才要求逐动作接纳。

## 6. 因果账本

### 6.1 唯一写入口

每个 Run 使用一个 SQLite WAL 账本。所有权威记录经同一个 appender 写入，生成单调递增的
`ledger_seq`。账本记录不可原地修改；纠正通过追加新的 supersede、contradict 或 reconciliation
记录完成。

账本条目至少包含：

```text
ledger_seq, event_id, run_id, attempt_id?, producer, trust_tier,
kind, schema_version, occurred_at, causation_id?, correlation_id?,
payload, artifact_refs[], prev_hash?, record_hash?
```

哈希链只用于发现意外损坏和投影漂移；宿主代码和宿主管理员仍然拥有最终控制权，因此不能把它
宣传成对抗宿主的密码学证明。

### 6.2 事件族

账本按语义分组，而不是按 UI 文件分组：

| 事件族 | 典型事件 |
| --- | --- |
| Run | admitted、provisioning、active、draining、verifying、terminal |
| Attempt | planned、starting、active、draining、finished |
| Authority | capability issued、lease granted/renewed/revoked、action denied |
| Budget | reserved、released、settled、exhausted |
| Action | admitted、dispatched、returned、failed、unknown、reconciled |
| Observation | captured、artifact committed、observation linked |
| Claim | claimed、superseded、contradicted |
| Evidence | evidence recorded、predicate verified/contradicted |
| Objective | verdict recorded、submission proposed/accepted/rejected |
| Operator | hint、cancel、reconcile、resume |

事件名和 schema 在实现计划中定稿；本设计只固定语义边界和禁止项。

### 6.3 投影而非双写

以下文件继续存在，但降级为可重建投影：

```text
world.db / world.snapshot.json     视图：由 evidence + claims 派生
world.context.txt                  视图：给 Agent 的有界读模型
trace.jsonl                        视图：面向操作者的追加式审计导出
progress.json                      视图：预算与统计快照
checkpoint.json                    兼容提示：从账本游标生成
result.json                        视图：终态读取模型
```

导入器、审计器和测试可以比较投影与账本序列；投影落后时从账本重建，而不是反向修复账本。

## 7. 运行协议

### 7.1 Run 状态机

```text
ADMITTED -> PROVISIONING -> RUNNING -> DRAINING -> VERIFYING -> TERMINAL
                                  \-> RECOVERING -> RUNNING
any nonterminal ---------------------> CANCELLED
```

终态只能是 `SUCCEEDED`、`FAILED`、`EXHAUSTED`、`CANCELLED` 之一。首次写入终态后必须拒绝
第二次终态转换，且终态投影由同一个账本事务产生。

目标状态和 Run 状态分开保存：

```text
objective_status = unavailable | inconclusive | contradicted | verified
```

一个 Run 可以正常进入终态而目标仍是 `inconclusive`；这比把“Agent 写完了报告”误报为
目标完成更诚实。

### 7.2 Attempt 状态机

```text
PLANNED -> STARTING -> ACTIVE -> DRAINING -> FINISHED
                  \-> FAILED
ACTIVE -> CANCELLED
```

Attempt 只拥有自己的 lease、会话游标和 scratch。Run 级别的提交、评测和终态不能被 Attempt
直接覆盖。

### 7.3 控制律

Supervisor 的输出只有四类：

```text
CONTINUE   继续允许 Agent 事件和已接纳动作
DRAIN      停止接纳新的危险动作，允许收尾与提交
VERIFY     进入客观评测
TERMINATE  进入唯一终态
```

输入只包括 deadline、预算剩余、目标 verdict、lease 状态、操作者命令和不安全 unknown action。
重复命令族、无进展计数和上下文长度可以作为 trace 指标，但不能成为 steer 或 stop 的依据。

## 8. Action 因果协议

### 8.1 接纳

Brokered action 在 dispatch 前完成：

1. 校验 producer、run、attempt、scope 和 effect class；
2. 申请预算预留；
3. 生成稳定 `action_id` 与 `idempotency_key`；
4. 追加 `action.admitted`；
5. 只有追加成功后适配器才允许发出请求。

如果预算不足、lease 失效、scope 不匹配或账本不可写，动作在 dispatch 前失败。失败本身也是
一条可审计记录。

### 8.2 生命周期

```text
ADMITTED -> DISPATCHED -> RETURNED
                      \-> FAILED
                      \-> UNKNOWN -> RECONCILED(succeeded|failed|unknown)
```

`execution_status` 回答“调用是否发生了什么”，`effect_status` 回答“是否被独立观察到”，
`objective_status` 回答“目标是否被授权方确认”。三者不得互相替代。

### 8.3 崩溃窗口

| 崩溃位置 | 结果 | 恢复规则 |
| --- | --- | --- |
| admitted 前 | 没有授权、没有副作用 | 可以重新接纳，必须生成新的 decision |
| admitted 后、dispatch 前 | 有意图，无已知副作用 | 只有 pure/read-only 或适配器确认未发出时可重试 |
| dispatch 后、响应前 | 副作用未知 | 不自动重试；使用同一幂等键查询或进入 unknown |
| 响应后、settle 前 | 副作用可能已发生 | 保留 unknown，查询外部状态后对账 |
| settle 后、投影前 | 账本已知道结果 | 只重建投影，不重发动作 |

对于容器内 lease effect，Harness 不一定能查询目标世界；此时 unknown 是一等结果，而不是一个
等待循环自动解决的临时状态。

## 9. 证据与验证

### 9.1 三类事实分开存储

```text
Claim       "我打到了 80 端口"        Agent，可错，可供 Agent 自己参考
Observation "connect() 返回 200"      Harness 授权观察者实际捕获
Evidence    "该 Observation 支持动作 A" 授权验证器建立绑定
Verdict     "目标已满足/未满足"        授权 evaluator 或 verifier 最终裁决
```

Agent 的 claim 进入 Attempt scratch 或账本中的 `claim` 事件，但不会自动进入 Evidence View。
只有被授权的观察者产生的 Observation 才能成为 Evidence 的引用对象。

### 9.2 验证只在必要边界发生

v3 取消“每次工具调用自动生成通用 action verdict”的热路径。并不是否认动作效果，而是承认：
没有独立观测时，Harness 无法对一个 shell 退出码判断目标语义。

验证分为两类：

1. **目标验证（必须）**：task verifier 或 benchmark evaluator 对 Run 目标签发 verdict。
2. **显式谓词验证（可选）**：Agent 或操作者请求一个只读 probe，观察者返回 Observation，
   授权 verifier 再生成 predicate verdict。

这样已执行、已观测、已验证就不再被压缩成一个含糊的 action status，也不会因为缺少预期观测
而向 Agent 反复注入无信息 steer。

### 9.3 真实性规则

- `EvidenceRef` 必须指向当前 Run 中不可变 artifact 的 hash 和账本位置。
- `verified` 必须有至少一个由对应 producer 签发且通过 schema/authority 检查的 Evidence。
- Objective verdict 的 producer 只能来自组合根显式注册的 verifier/evaluator。
- 平台返回 “submission accepted” 只是提交承诺被接受，不等于目标成功。
- 投影可以显示 evidence，但不能把 claim 的字体、颜色或摘要当作已验证。

## 10. 预算、租约与权限

### 10.1 预留式预算

预算账本记录 `reserved`、`settled`、`released` 三种状态。模型或工具调用在 dispatch 前申请
预留；成功后按实际值结算，失败则释放未使用部分。未知成本使用声明的上限预留，不能先发出再
祈祷不会超支。

Run 预算不可被 Attempt 提升；Attempt 只能消费 Run 授予的份额。Wall clock 由 Supervisor 持有，
不做会话切分，以避免并发的多个会话各自拥有完整赛窗。

### 10.2 Lease 与 fencing

每个 Attempt 获得带过期时间和单调 fencing token 的 lease。资源适配器必须拒绝旧 token 的
操作，防止恢复进程和旧进程同时拥有同一个环境。lease 失效后，旧会话只能做只读收尾，不能提交
新的危险动作。

### 10.3 Capability

Capability 是组合根签发的内部授权对象，至少绑定：

```text
run_id, attempt_id, resource, verbs, scope, expires_at, lease_fencing_token
```

Agent 容器永远拿不到账本写权限、宿主凭据或 verifier capability。Gateway 和 adapter 只能拿到
当前动作所需的短期 capability。

进程内 capability 不是对抗被攻陷宿主的密码学边界，但它能阻止代码路径意外升级信任，并为将来
的进程隔离留下清晰接缝。

## 11. 竞争、提交与目标收敛

Run 级别维护一个提交去重集合和一次最终 evaluator。每个 Attempt 可以提出 candidate，但
candidate 先成为 Run 级 commitment action：

1. 对 candidate 内容取稳定 hash；
2. 同一 hash 只提交一次；
3. 记录 accepted/rejected 和 evaluator 的 observation；
4. 只有最终 objective verdict 为 `verified` 才关闭全部 Attempt；
5. 单个 Attempt 的结束、超预算或会话失败不自动终止其他 Attempt。

这样“多个会话竞争”是 Run 的调度策略，而不是多个真相来源。结果投影仍可报告每个 Attempt 的
模型、预算、提交和摘要，但这些是 Run 的观测维度，不是独立终态。

## 12. 投影与 Agent 上下文

### 12.1 投影族

| 投影 | 内容 | 写入者 |
| --- | --- | --- |
| Evidence View | 已验证 facts、开放 claims、矛盾、失败和过期状态 | Harness projector |
| Attempt Scratch | Agent 自己的笔记、原始工作区、未验证假设 | Agent |
| Context | 目标、规则、预算、证据摘要、开放 claims、最近动作、操作者提示 | Harness projector |
| Trace | 因果链、授权、预算、动作、观测和终态 | Harness projector |
| Result | 对外结果、目标 verdict、提交和 metrics | Harness projector |
| Audit | 账本重放摘要、投影 hash、未知动作和对账状态 | Harness projector |

### 12.2 上下文原则

Context 是有界、确定性和可解释的读模型：

- 先给目标和硬边界，再给证据事实，再给 Agent 自己的开放 claims；
- 展示 action 的 `execution_status`、`effect_status` 和 `objective_status` 的差别；
- 只显示与当前问题相关的失败和 ruled-out 路径；
- 不把 world revision 变化、pending verdict 或原始工具流水当成 steer；
- 操作者提示是方向输入，不是目标 verdict；
- 相同账本前缀和投影版本必须生成字节稳定的上下文。

Agent 的 scratch 可以自由增长，但 Harness 只按显式限额读取和投影；也不会解析笔记来生成计划。

## 13. 恢复协议

### 13.1 启动恢复

恢复一个 Run 时：

1. 获取 Run 的独占恢复 lease 和 fencing token；
2. 从账本重放到最后完整记录；
3. 校验未终结的 lease、动作和预算预留；
4. 对 unknown action 执行适配器查询或标记为需人工对账；
5. 只有不存在未解决的危险未知动作时，才启动新的 Attempt epoch；
6. 重新生成所有投影，而不是信任旧投影。

### 13.2 不允许的恢复

- 不根据 Agent 报告“可能成功”而重放危险动作；
- 不把旧 checkpoint 当作比账本更新的事实；
- 不让旧 lease 使用新 token 继续写入；
- 不在未知副作用存在时自动推进目标状态；
- 不用重复发送 submission 来“确认”第一次是否到达。

## 14. 安全边界

v3 延续 v2 的容器和凭据边界，并增加因果层的明确边界：

1. Agent 容器不能写账本、artifact store、result、checkpoint 或 verifier capability；
2. Agent 只能写自己的 scratch、事件 inbox 和提交 inbox；
3. 所有 inbox 都按恶意输入处理，必须限行、限字节、限 schema；
4. 模型和工具凭据只存在于宿主侧 Gateway/adapter；
5. 网络 profile、挂载、capability 和 runtime 由 lease 固化，不得在会话中途隐式放宽；
6. verifier 和 evaluator 的结果必须引用不可变 artifact；
7. operator hint 是可信方向输入，但不是验证；
8. 容器、benchmark、模型和工具输出不能改变宿主控制律。

## 15. 目标模块形状

以下是 v3 的目标职责图，不是要求立即搬动现有模块。迁移期间保留现有 import 路径和事件 schema，
直到版本化迁移和回归测试完成。

```text
entrypoints/       CLI、Gateway、报告命令
application/       RunService、AttemptScheduler、Provisioner、Teardown
kernel/            Ledger、Record、RunState、Supervisor、BudgetPort
authority/         Capability、Lease、Policy、Fencing
evidence/          Artifact、Observation、Evidence、Verdict
projections/       WorldView、Context、Trace、Result、Audit
adapters/          Pi、Docker、TSec、task verifier、model provider
```

依赖方向：

```text
entrypoints -> application -> kernel/authority/evidence/projections
                                 ^
                                 |
                              adapters
```

kernel 不得导入 FastAPI、Docker、subprocess、TSec SDK 或 Pi 实现；adapter 不得直接决定 Run
终态；projection 不得成为任何写路径的权威。

## 16. 迁移计划

### M0. 设计冻结

保留 v2 生产路径。新增 ADR 记录本设计中的不可变假设、事件族和验收口径。此阶段不改公共 schema。

### M1. 影子账本

在现有 Run 旁边写入 v3 账本，但不改变 v2 行为。对同一 Run 比较 World、trace、progress、
checkpoint 和 result 的投影结果。账本写入失败时明确告警，不改变现有运行结果。

验收：回放历史 trace 和至少一轮本地端到端运行，账本重放与现有投影在目标、动作、预算、提交和
终态上等价。

### M2. 投影换源

让 World View、Context、Trace、Progress 和 Result 从账本派生。旧的 SQLite World 存储作为
导入器/兼容层保留。此阶段先迁移只读投影，再迁移 checkpoint 游标。

验收：相同账本前缀重复生成相同投影；投影删除后可从账本重建；旧 `harness/world/v1` 记录可导入。

### M3. 接纳与预留

为 brokered action、模型调用和 verifier 调用加入预算预留与 lease fencing。Pi 容器内工具在这
一阶段仍由容器 lease 授权，但所有可观测调用必须有统一 action_id。

验收：预算不足时在 dispatch 前拒绝；崩溃窗口不导致双计费；旧 lease 的动作被拒绝。

### M4. 验证降噪

把每工具调用的通用 `ActionVerdict` 从控制脊移出；保留执行记录、Observation 和显式谓词验证。
目标 verdict 成为唯一必需的收尾验证。

验收：历史 pending verdict 夹具回放时不产生额外 steer；目标成功由独立 evaluator/verifier 决定。

### M5. 应用层收敛

把 Orchestrator 和 BenchmarkRunner 的共享生命周期合并到 RunService/AttemptScheduler；把
Pi、Docker、TSec 和 verifier 的翻译逻辑留到 adapter。单个文件不再同时拥有生命周期、预算、
证据和设备操作。

### M6. 清理

关闭影子模式，删除重复权威文件，保留版本化导入器和兼容投影。只有完成真实 sweep、崩溃矩阵和
离线回放后，才把 v3 标为生产架构。

回滚策略：M1-M4 通过 feature flag 保持 v2 可运行；任何阶段发现投影不等价、预算漂移或未知
动作无法对账，停止推进并保留账本与原始工件作为证据。

## 17. 验收标准

### 17.1 因果与恢复

- [ ] 账本单一写入：任意 Run 的权威状态只来自一条追加式账本。
- [ ] 五个崩溃窗口逐一注入：admitted 前、dispatch 前、dispatch 后、响应后、settle 后。
- [ ] unknown action 不会自动重放；对账后仍有唯一终态。
- [ ] 旧 lease 的写入被 fencing token 拒绝。
- [ ] 投影删除后可由账本完整重建。

### 17.2 信任

- [ ] Agent 事件、模型输出和工具原始输出无法直接生成 `evidence` 或 `verified`。
- [ ] 伪造 producer、跨 Run evidence、过期 artifact 和错误 revision 全部 fail closed。
- [ ] submission accepted 不等价于 objective verified。
- [ ] Agent 容器不能读取或写入账本、result、checkpoint、verifier capability 和宿主凭据。

### 17.3 预算与竞争

- [ ] 预算预留发生在 dispatch 前；被拒绝的动作没有外部副作用。
- [ ] 预算结算在崩溃后可重放，不重复计费或重复释放。
- [ ] 同一 candidate hash 只提交一次。
- [ ] 单个 Attempt 失败不会覆盖 Run 终态，也不会阻止其他 Attempt。
- [ ] 任一目标 verdict 成功只产生一个 Run 终态。

### 17.4 行为回归

- [ ] 使用 2026-10-16 的历史 trace 片段，v3 投影能重建相同的提交、预算、目标状态和终态。
- [ ] pending/unavailable 不再产生 Agent steer。
- [ ] Context 保持有界，并对相同账本前缀稳定。
- [ ] 离线测试覆盖无模型凭据、无 Docker、无 TSec 的纯控制律路径。

### 17.5 工程质量

- [ ] kernel/evidence/projection 不导入基础设施适配器。
- [ ] CONTROL_SPINE 的每个新增模块必须在审查中说明它如何改善闭环，而不是只做转发。
- [ ] 1 万条合成事件的账本重放与投影重建在 CI 可接受预算内；具体上限由 M1 基线测量后在实现计划中冻结。
- [ ] 迁移期持续运行现有 ruff、pytest、architecture 和 docs consistency 检查。

## 18. 非目标

- 不做微服务、消息队列、分布式共识或跨主机 worker 集群；
- 不把 Agent 规划成 Harness 内的 planner、DAG、技能排序器或角色编排；
- 不承诺容器内 shell 的逐命令前置审批；
- 不承诺 exactly-once；只承诺可识别的 at-most-once 接纳、幂等键和 unknown 对账；
- 不把哈希链宣传成对抗恶意宿主的证明；
- 不建立完整目标世界模型、向量数据库或神经世界模型；
- 不把 benchmark 专属生命周期写进 kernel；
- 不为减少代码行数而牺牲因果身份和恢复边界。

## 19. 实施计划需要决定的事项

以下属于实现计划，不应在架构层偷定：

1. 账本事件的具体 schema、版本号和 SQLite 表结构；
2. artifact store 的 hash/路径布局和清理策略；
3. Pi 工具事件能否提供 dispatch 前钩子，以及 lease effect 的最小可观测字段；
4. RunService 与现有 Orchestrator/BenchmarkRunner 的分阶段提取顺序；
5. M1 影子账本的 feature flag、开关默认值和回滚操作；
6. 以哪几条历史 TSec trace 和离线 fixture 作为投影等价的 golden set；
7. 1 万事件重放和上下文生成的实际性能基线。

## 20. 与 v2 的关系

v2 解决了“认知不要被 Harness 抢走”。v3 解决“因果和信任不能被多个文件、隐式状态和模糊裁决
稀释”。两者的共同底线不变：

> 模型可以自由地探索，但不能自由地定义事实、权限和成功。

v2 仍是当前实现和验收基线；v3 只有完成影子账本、投影等价、恢复矩阵和真实 sweep 后才成为
新基线。本文不是立即重写代码的许可，而是后续实现计划和迁移评审的规格。
