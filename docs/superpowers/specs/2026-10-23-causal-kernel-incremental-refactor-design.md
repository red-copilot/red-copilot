# 因果内核增量重构设计（方案 B）

> 状态：待审批设计稿。目标架构来源为
> [第一性原理架构 v3](2026-10-22-first-principles-architecture-design.md)，实施方式选择
> “因果内核优先、增量替换 v2”，不是立即把 `ledger.db` 切成唯一权威。
>
> 设计基线：`5bfad06`；该快照 `pytest -q` 为 513 passed，`ruff check .` 通过。`origin/main`
> 在评审时领先一个只涉及 TSec 竞争优化的同步提交，不改变本文的运行时边界结论。

## 1. 意图与为什么

Harness 的长期价值不是“再包一层 Docker”，而是让一次运行能够回答：

1. 哪个 Run、Attempt、Action 被授权；
2. 副作用是否真的越过了宿主边界；
3. 哪个 Observation 被谁捕获；
4. 哪个 Evidence 把 Observation 绑定到哪个命题；
5. 哪个授权 Verdict 足以结束目标；
6. 崩溃后哪些动作可以重放，哪些必须保持 unknown。

当前 v2 已经正确地把认知留给 Agent，把环境、信任、记账和恢复留在 Harness。但权威事实仍
分散在 `world.db`、`trace.jsonl`、`progress.json`、`checkpoint.json` 和 `result.json` 中；
工具事件在 Pi RPC 输出后才获得 action identity；通用 `ActionVerdict` 仍参与热路径；预算仍是
事后软上限。继续增加字段会让这些不一致更难解释，所以需要先把因果身份和授权边界变成内核
契约，再逐层替换投影。

这次重构不追求“架构更漂亮”，而追求三个可验证收益：

- 任何 `verified` 都能沿 Evidence、Observation、Action、Attempt、Run 反向追溯；
- 账本不可写或动作状态未知时，系统明确失败，不猜测成功；
- 旧 v2 路径在迁移期间仍可运行，并可用历史运行验证投影等价。

## 2. 目标、非目标与反目标

### 2.1 目标

1. 建立唯一的每 Run 追加式因果账本写入端口。
2. 显式建模 Run、Attempt、Action、Observation、Claim、Evidence、Verdict。
3. 对跨宿主副作用和模型调用，在 dispatch 前完成授权与预算预留。
4. 用动作绑定的 Observation 代替“最新观测”推断。
5. 让 World、Context、Progress、Trace、Result、Audit、Checkpoint 成为可重建投影。
6. 把生命周期从 `Orchestrator` 与 `BenchmarkRunner` 收敛到应用层 `RunService` 和
   `AttemptScheduler`，适配器只做翻译和资源操作。
7. 用故障注入、离线回放和真实 sweep 证明迁移，而不是只证明单元测试通过。

### 2.2 非目标

- 不拆微服务，不引入消息队列、分布式共识或跨主机 worker；
- 不承诺 exactly-once；
- 不承诺对容器内每条 shell 命令做逐命令前置审批；
- 不把哈希链宣传成对抗恶意宿主的密码学证明；
- 不恢复 Harness 侧 planner、技能排序、角色编排或 DAG；
- 不把 benchmark 专属生命周期写进 kernel；
- 不为减少行数牺牲因果身份、授权边界和恢复能力。

### 2.3 反目标

以下情况即使测试全绿也视为失败：

- 影子账本永远停留在 shadow，旧权威文件永不删除；
- 为了兼容而允许 claim 升级为 evidence，或让 submission accepted 冒充 objective verified；
- 为了性能跳过动作接纳、预算预留或 unknown 对账；
- 把 Agent 输入当控制指令，或让适配器直接决定 Run 终态；
- 用一个更复杂的抽象层包装现有混乱，而没有减少权威来源。

### 2.4 适用范围与显式例外

完整因果保证适用于 `harness run` 和 `harness tsec` 创建的 Run。authoritative 模式要求所有模型
调用经过 run-bound Gateway，以便在 dispatch 前预留预算并绑定 Run。

- direct-provider Pi 模式只作为迁移期 legacy 模式保留，Stage 6 前不得在 authoritative 模式中
  声称拥有完整的模型预算预留和因果保证；
- 独立 `harness gateway` 与 `harness subagent` 不是 Run，明确排除在因果内核保证之外；
- 被排除的路径仍必须遵守既有凭据、容器和输出脱敏边界，但不得签发 kernel 的 Evidence 或
  ObjectiveVerdict，也不得复用 Run 级预算/恢复语义；
- 若未来需要把这些路径纳入因果保证，必须单独设计 Run 包装和版本化迁移，不通过隐式降级混入。

## 3. 不可变约束

1. **Agent 不可信。** Agent 文本、模型输出、工具原始输出、任务内容和提交候选都按恶意输入处理。
2. **真相有生产者。** Claim、Observation、Evidence、Verdict 不可互相替代。
3. **未知不重放。** 外部副作用状态未知时，只能查询、独立观测或人工对账。
4. **账本先行。** 权威状态变更必须先追加账本；派生文件不得反向修复账本。
5. **投影可重建。** 相同账本前缀、投影版本和配置必须生成相同视图。
6. **失败关闭。** 账本、schema、证据引用、lease 或适配器状态不明确时，不得猜测成功。
7. **认知归 Agent。** Harness 的控制律只读取外部可判定条件。
8. **边界不越过模块。** kernel/evidence/projection 不导入 FastAPI、Docker、subprocess、httpx
   或 TSec SDK；adapter 不直接决定终态。

## 4. 现状与具体缺口

### 4.1 已具备的基础

- `LedgerStore` 已有每 Run SQLite WAL、单调序号、事件唯一性、哈希链、append-only trigger；
- `TraceRecorder` 已能把 v2 trace 镜像到影子账本，并把失败隔离到 `ledger.error.json`；
- `VerifierRegistry` 已用进程内 capability 区分授权 verifier；
- `ObjectiveVerdict` 已独立于 action 结果，agent claim 不能直接完成目标；
- `RunLifecycle` 已提供显式相位机；
- `WorldRepository`、`ProgressLedger`、`SolverLoop` 和 `TraceRecorder` 已有较好的测试边界。

### 4.2 必须解决的结构问题

1. **双权威。** 账本只是 `trace.jsonl` 的镜像；World、Progress、Checkpoint、Result 仍从 v2
   文件读取。
2. **因果后置。** Pi 工具事件在观测后才生成 `tool_call_id`；账本无法证明 dispatch 前发生过
   授权，也无法证明外部副作用是否已发出。
3. **观测错配。** `ActionVerifier` 取“当前修订点之前最新 Observation”，不是由当前 action
   产生的 Observation。多会话共享 World 时，时间相关不等于因果相关。
4. **通用 ActionVerdict 污染热路径。** 每个工具调用都生成 status，导致大量 pending/unavailable
   记录和无信息 steer；目标验证和动作效果验证没有彻底分层。
5. **软预算。** `BudgetMonitor` 在超限后开 grace window，正在执行的模型/工具调用仍可能越界；
   预算没有 dispatch 前预留和崩溃后可重放的结算。
6. **恢复只有“辅助记录”。** checkpoint 明确不是 exactly-once；未知动作仍需 operator
   reconciliation，且 Docker 故障矩阵未完成。
7. **应用层职责重叠。** `BenchmarkRunner` 同时负责 provision、attempt 调度、提交去重、预算、
   证据和终态；`Orchestrator` 维护另一套本地生命周期。

## 5. 方案比较与选择

### 方案 A：账本优先，直接切 v3

把 `ledger.db` 立即设为唯一权威，所有投影同步换源。

- 优点：单一真相，概念最干净；
- 缺点：同时改变写路径、读路径、恢复协议和信任边界，现有测试不足以证明迁移安全；
- 结论：不作为第一刀。

### 方案 B：因果内核优先，增量替换 v2

先建立 `RunKernel` 的追加、授权、状态机和 projection 端口，保持 v2 读路径可运行；通过逐层
替换和故障注入完成迁移。

- 优点：每阶段可回滚、可验收；比赛路径不必停摆；能先修复最危险的因果缺口；
- 缺点：一段时间内存在受控双写和兼容层；
- 结论：采用。

### 方案 C：保留 v2，只修当前风险

只修 Observation 绑定、预算预留和恢复矩阵。

- 优点：短期成本最低；
- 缺点：多权威文件和不一致仍存在，后续仍要重做迁移；
- 结论：作为风险缓解，不作为最终架构。

## 6. 目标结构

目标不是立即搬目录，而是先冻结职责，再按依赖方向迁移。最终形状：

```text
entrypoints/      CLI、Gateway、报告命令
application/      RunService、AttemptScheduler、Provisioner、Teardown
kernel/           Ledger、KernelEvent、Run/Attempt/Action 状态机、Appender
authority/        Capability、Lease、Policy、Fencing、BudgetReservation
evidence/         Observation、EvidenceRef、ObjectiveVerdict
projections/      World、Context、Trace、Progress、Result、Audit、Checkpoint
adapters/         Pi、Docker、TSec、task verifier、model provider
```

依赖方向：

```text
entrypoints -> application -> kernel / authority / evidence / projections
                                      ^
                                      |
                                   adapters
```

约束：

- kernel 不导入任何基础设施实现；
- application 是唯一组合根，拥有授权注册、预算策略、Run 终态和 teardown；
- projection 只读账本，不写权威事实；
- adapter 只翻译协议、管理资源、回报结果，不签发 objective verdict；
- 旧 import path 在迁移期保留，用兼容包装器指向新实现。

## 7. 核心契约

### 7.1 KernelEvent

沿用现有 `harness/ledger-store/v1` 和 `harness/ledger-event/v1`，不为了重构先改存储格式。
新增语义使用版本化 payload：

- `harness/run-state/v1`
- `harness/attempt-state/v1`
- `harness/action/v1`
- `harness/budget-reservation/v1`
- `harness/observation/v1`
- `harness/evidence-ref/v1`
- `harness/objective-verdict/v1`

每一条记录至少包含 v3 已冻结字段：`ledger_seq`、`event_id`、`run_id`、可选 `attempt_id`、
`producer`、`trust_tier`、`kind`、`schema_version`、`occurred_at`、`causation_id`、
`correlation_id`、`payload`、`artifact_refs`、哈希字段。

写入规则：

- 每 Run 一个 appender；
- 复用 SQLite WAL、`BEGIN IMMEDIATE`、唯一 `event_id` 和 append-only trigger；
- 需要原子发生的“动作接纳 + 预算预留”通过一个批次事务提交；
- 账本写入失败时，在 authoritative 模式下不得 dispatch 危险动作；
- 哈希链只用于发现损坏和投影漂移，不作为恶意宿主证明。

### 7.2 Run、Attempt 与 Action

```text
Run
├── objective + evaluator + run budget + terminal record
├── Attempt A ── lease A ── session A ── scratch A
├── Attempt B ── lease B ── session B ── scratch B
└── run-level commitment channel: submission dedupe + evaluator
```

`Action` 必须属于唯一的 Run；容器内动作还必须属于某个 Attempt。竞争会话共享 Run 级提交
去重和最终 evaluator，但每个 Attempt 拥有自己的容器、scratch、checkpoint hint 和预算份额。

这里的 Attempt 是预声明的执行槽位，不是每次重连临时创建的对象：

- 单 solver 运行有一个 Attempt；`AgentSpec.executors` 的每个竞争会话各有一个 Attempt；
- `attempt_id` 在 Run 内稳定，用于预算份额、动作归属、Observation 归属和审计；
- Pi context restart、重连或同容器内重建 transcript 时递增 `session_epoch`，不新建 Attempt；
- lease 使用 `attempt_id + lease_epoch` 做 fencing；新 lease 必须递增 epoch；
- Stage 0-5 不引入失败后自动重试生成新 Attempt；未来若允许重试，必须显式创建新 Attempt 并受
  `max_attempts` 策略约束。

### 7.3 Action 分类

| 类别 | 例子 | 授权点 | 迁移策略 |
| --- | --- | --- | --- |
| `lease_effect` | Pi shell、文件、目标网络请求 | 容器 lease | 观测后补 action identity；不假装逐命令前置审批 |
| `brokered_effect` | benchmark 提交、宿主 verifier、外部 API | Action Broker | dispatch 前接纳并预留 |
| `model_effect` | 模型请求与计费 | Gateway | dispatch 前预留，按 usage 结算 |
| `observation_effect` | 只读 probe、rendered observation、evaluator 查询 | Observer capability | 保留 action identity，允许安全重放 |

Action 生命周期：

```text
ADMITTED -> DISPATCHED -> RETURNED
                     \-> FAILED
                     \-> UNKNOWN -> RECONCILED(succeeded|failed|unknown)
```

`execution_status`、`effect_status` 和 `objective_status` 分属不同记录，不得互相替代。

### 7.4 Observation、Evidence 与 Verdict

`ObservationRecord` 增加显式因果字段：

```text
observation_id, run_id, attempt_id?, action_id?, producer,
captured_at, world_revision, kind, summary, artifact_ref?, artifact_hash?
```

规则：

- authoritative 模式不再使用“最新修订点之前的 Observation”作为动作基线；
- 一个 Observation 只有显式 `action_id` 或明确的 objective probe/correlation 才能绑定到动作；
- `EvidenceRef` 引用 Observation 的不可变 artifact/hash，并绑定 subject、action 或 predicate；
- `ObjectiveVerdict` 只来自已注册 `task_verifier`/`benchmark_evaluator`；
- submission accepted 只是 submission feedback，不是目标成功；
- 旧 `ActionVerdict` 保留为兼容读取和诊断，但在 M4 后不进入控制脊；
- 若 Run 到期、取消或无法继续时仍有危险 unknown action，Run 可以进入终态，但终态记录必须
  带上 unresolved unknown action IDs，且 objective status 不得为 `verified`；之后的操作者对账记录
  可以追加，但只能补充未知动作的结论，不能重新打开或改写 Run 终态。

### 7.5 预算与 lease

- `model_effect`：dispatch 前预留一次 model call；token/cost 预留使用声明的 `max_output_tokens`
  或配置的最坏上限。无法计算有限上限时，authoritative 模式拒绝请求；shadow 模式记录
  “would reject”。
- `brokered_effect`：dispatch 前预留一个 tool-call 单位；失败或取消必须释放。
- `lease_effect`：以 Attempt lease 为边界分配工具调用池。由于 Pi 没有逐命令前置 hook，
  允许一次已观测调用越过软池后立即停止继续 dispatch；不得声称严格零越界。
- 结算记录必须幂等：重复 settle 只确认同一 reservation，不重复计费。
- wall time 使用 Run 级绝对 deadline，不做预算换算。

### 7.6 投影

下列文件继续存在，但只作为投影：

```text
world.db / world.snapshot.json
world.context.txt
trace.jsonl
progress.json
checkpoint.json
result.json
audit report
```

每个投影记录 `projection_version`、`ledger_head`、`config_hash`。投影落后时从账本重建；
不同投影之间不要求跨文件原子一致，因为任何不一致都不能反向定义真相。

Context 的确定性定义为：给定账本前缀、投影版本、配置和显式 `as_of` 时刻，输出字节稳定。
实时剩余墙钟属于独立的 ephemeral clock 输入，不进入可重建 Context；需要 golden 比较时固定
`as_of`，实时界面另行注入当前时钟。

## 8. 迁移阶段

### Stage 0：冻结基线与 golden set

- 记录当前主路径的模块边界、CONTROL_SPINE、源码行数；
- 固定 2026-10-16 历史 trace 片段和至少一个多会话 fixture；
- 建立投影等价比较基线、账本重放基线和性能基线；
- 写 ADR，明确“authoritative 模式失败关闭”的开关和回滚方式。

退出条件：基线可重复，golden set 可离线运行。

### Stage 1：抽出 kernel 端口，保留 v2 读路径

- 新增 `RunKernel`/`LedgerAppender` 接口，包装现有 `LedgerStore`；
- `TraceRecorder` 改为通过 kernel 端口写，而不是直接操作 ledger；
- 补上批次追加和 schema 校验；
- 保持 `HARNESS_LEDGER_SHADOW` 的既有语义，默认仍是 shadow。

退出条件：账本写入、失败隔离、重复 ID、哈希链和 v2 trace 等价测试全绿。

### Stage 2：动作接纳与预算预留

- 为 model call、submission、verifier 调用加入 dispatch 前接纳；
- 记录 `action.admitted` 与 `budget.reserved`；
- 建立 reservation 的 release/settle 和 unknown 对账；
- 为 Pi 工具建立 lease effect 的观测后 action identity。

退出条件：账本不可写、预算不足、lease 冲突或重复 settle 时，危险动作不会被 dispatch。

### Stage 3：证据绑定与控制脊降噪

- Observation 必须显式绑定 action/correlation；
- 禁止用“最新 Observation”推断动作效果；
- 目标 verdict 只接受 evaluator/verifier capability；
- 把通用 `ActionVerdict` 从控制脊移到诊断投影；
- 保留 Agent 可读的 execution/effect/objective 区别。

退出条件：跨会话观测不能验证另一个会话的动作；无独立观测时不产生 verified；无信息 verdict
不再 steer。

### Stage 4：投影换源

建议顺序：

1. Trace 与 Audit；
2. Result 与 sweep summary；
3. Progress 与 Context；
4. World read model；
5. Checkpoint 游标。

每一步都保留旧投影读取作为 fallback，并验证“删除投影后从账本重建”。World/Context 最后切换，
因为它们会影响 Agent 行为。

退出条件：相同账本前缀的投影字节稳定；旧 v2 记录可导入；重建结果与 golden set 一致。

### Stage 5：应用层收敛

- 提取 `RunService`：目标、预算、submission 去重、终态、teardown；
- 提取 `AttemptScheduler`：lease、session、preemption、失败隔离；
- `BenchmarkRunner` 和 `Orchestrator` 只负责入口差异和适配器选择；
- TSec/Verifier/Gateway 通过 port 接入，不进入 kernel。

退出条件：本地 run 和 benchmark run 共享同一套生命周期测试；单个 Attempt 失败不会覆盖 Run
终态；teardown 幂等。

### Stage 6：关闭 shadow，删除旧权威路径

删除条件全部满足后才允许：

- 账本作为唯一权威，投影均可重建；
- 五个崩溃窗口均有故障注入证据；
- 未知动作不会自动重放；
- 真实 sweep 通过，提交率、time-to-first-submission、steer 数量和预算误差可接受；
- 10k 事件回放和上下文重建达到冻结的性能上限；
- 旧 import path 有版本化迁移或兼容包装。

最终清理旧双写、旧 checkpoint 权威读路径、通用 action verdict 热路径和不必要的兼容层。

## 9. 验收与测试矩阵

### 9.1 信任

- 伪造 producer、跨 Run evidence、过期 artifact、错误 revision 全部 fail closed；
- Agent 文本和工具退出码不能成为 Evidence；
- submission accepted 不能成为 objective verified；
- 旧 lease 不能写入新 Attempt 的权威状态；
- 容器无法读写账本、result、checkpoint 和 verifier capability。

### 9.2 因果与恢复

- admitted 前、dispatch 前、dispatch 后、响应后、settle 后五个窗口逐一注入；
- 账本不可写时危险动作不 dispatch；
- reservation 崩溃后不重复计费或重复释放；
- unknown action 不自动重试，对账后只有一个终态；
- 投影删除后可完整重建。

### 9.3 竞争与会话

- 多 Attempt 共享 provision、submission dedupe 和 evaluator；
- 一个 Attempt 的 Observation 不能越过 correlation 验证另一个 Attempt 的动作；
- 首个 objective success 只产生一个 Run 终态；
- 单个 Attempt 失败、超时或取消不会覆盖其他 Attempt 的 Run 状态。

### 9.4 行为回归

- 2026-10-16 历史 trace 的提交、预算、目标状态和终态可重建；
- pending/unavailable 不再产生 Agent steer；
- Context 有界，且相同账本前缀生成稳定内容；
- 无模型凭据、无 Docker、无 TSec 的控制路径可离线运行；
- Docker-backed 恢复矩阵和一次真实 sweep 作为发布门，不以本地 skip 代替。

### 9.5 工程质量

- kernel/evidence/projection 不导入基础设施适配器；
- 新增控制脊模块必须在架构测试中显式登记；
- 每个阶段都运行 `ruff check .`、全量 `pytest -q`、架构测试和 docs consistency；
- 1 万事件合成账本的重放与投影重建有实测上限，达到上限后才能进入下一阶段。

## 10. 兼容、回滚与数据迁移

- 存储 schema 先保持 `harness/ledger-store/v1` 和 `harness/ledger-event/v1`；新增字段通过
  payload schema 版本扩展，不在第一阶段破坏公共事件格式。
- 旧 `world.events.jsonl`、`progress.json`、`checkpoint.json` 通过版本化 importer 进入账本；
  importer 必须可重放、可报告冲突，不能静默覆盖。
- Stage 1-4 保留 v2 fallback；任一投影不等价、预算漂移或 unknown 无法对账，停止推进并保留
  账本、原始工件和比较报告。
- 回滚只回滚读路径或 feature flag，不回滚已追加事实；账本是审计证据，不能为了提高成功率被
  删除或改写。
- 不保证迁移前非终态 Run 的跨版本恢复。升级前必须在旧版本上 drain 或终止这些 Run；迁移只
  导入、审计和 replay 已完成 Run。mixed v2/ledger 的在途迁移明确不在本设计范围内，避免用一个
  不确定的恢复协议同时迁移两套状态。
- `result.json`、`trace.jsonl` 的公共读取 schema 在迁移期保持兼容；新增字段采用版本化扩展。
- 不改变现有 result status 的长期公共含义（`finished|timeout|budget_exceeded|objective_completed`
  及错误路径）；Run phase 和 objective status 作为附加字段暴露。需要新终态语义时通过版本化字段
  或新 schema，不在原字段上重解释。

## 11. 风险与取舍

| 风险 | 取舍 | 缓解 |
| --- | --- | --- |
| 双写期间账本和 v2 漂移 | Stage 1-4 暂时接受受控双写 | 每阶段比较、fallback、删除条件 |
| 单 SQLite writer 成为瓶颈 | 每 Run 单 writer，接受局部串行 | 批量追加、WAL、性能基线 |
| Pi 无逐命令 hook | 不承诺容器内动作前置审批 | lease effect + 观测后 identity + 硬容器边界 |
| 预算预留过于保守 | 可能导致请求提前拒绝 | shadow 先测量，authoritative 使用明确上限 |
| 投影切换改变 Agent 行为 | World/Context 最后切换 | golden context、字节稳定、行为回归 |
| 兼容层长期存在 | 增加复杂度 | 每个兼容层写明删除阶段和验收 |
| 哈希链被误解为防篡改 | 不夸大安全属性 | ADR 和文档明确宿主管理员仍可重写 |

## 12. 我做的决定

以下是我建议的实现选择，用户可在审批时否决：

1. 采用方案 B，不立即切掉 v2 读路径。
2. 保留现有 ledger store/event schema，先通过 payload 版本扩展。
3. 每 Run 单 SQLite writer，保持 modular monolith。
4. 容器内 shell 不做逐命令前置审批；以 lease 为授权边界，明确允许一次观测越界。
5. 投影切换顺序为 Trace/Audit → Result → Progress/Context → World → Checkpoint。
6. `RunService` 与 `AttemptScheduler` 作为应用层组合根，不把生命周期放进 kernel。
7. authoritative 模式中，没有可绑定 Observation 的动作不能产生 verified；宁可 unavailable。
8. Attempt 是预声明执行槽位；context restart 只递增 `session_epoch`，不改 `attempt_id`。
9. 未解决的危险 unknown action 不阻止 Run 到期关闭，但 Run 必须显式携带 unknown IDs，且不能
   得到 verified objective。
10. result 的现有 status 语义保持兼容；Run phase 和 objective status 作为附加字段。
11. 可重建 Context 使用显式 `as_of`；实时墙钟是独立 ephemeral 输入。
12. authoritative 模式强制 run-bound Gateway；direct-provider 仅保留为迁移期 legacy。
13. 迁移前非终态 Run 不跨版本恢复；升级前 drain，迁移只处理已完成 Run 的导入、审计和 replay。

## 13. 留给实现计划决定的事项

1. `KernelEvent` 各 payload 的精确字段、默认值和 JSON schema；
2. `LedgerAppender.append_batch` 的事务接口和错误类型；
3. model token/cost 预留的默认上限与配置位置；
4. artifact store 的 hash 路径、清理和保留策略；
5. Pi 工具事件的 correlation 规则，以及 legacy event 的兼容窗口；
6. 历史 golden set 的具体 run、fixture 和投影等价比较算法；
7. 10k 事件回放、上下文重建和账本增长的性能上限；
8. `RunService` 与 `AttemptScheduler` 的提取顺序和模块名；
9. feature flag 的命名、默认值、回滚操作和观测指标；
10. Docker/Kubernetes 资源清理与崩溃恢复的具体故障注入方式。

## 14. 审批门

本设计获批后才能进入实现计划。实现计划必须：

- 按 Stage 0-6 拆分，每阶段有独立验收和回滚；
- 先写失败测试，再改控制脊；
- 不把 benchmark 专属逻辑放进 kernel；
- 在 Stage 4 前不声称 v3 已成为生产架构；
- 在真实 sweep、崩溃矩阵和投影重建全部通过前，不关闭 shadow。
