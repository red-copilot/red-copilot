# Red Harness 控制论

> 状态：工程基线。日期：2026-10-25。本文不是当前实现的完成证明，而是控制闭环的规范性模型、
> 设计评审基线和不变量证据索引。项目为什么存在、哪些权限不可让渡，以
> [first-principles.md](first-principles.md) 为准；实现是否满足本文，以代码、测试、故障注入和
> 真实评测为准。

## 1. 定位

Red Harness 使用控制论作为工程语言，但不把 agent 行为伪装成可精确建模的线性系统：

- 这里的“控制”指权限、预算、证据、恢复和终止的可判定边界，不是 PID、reward shaping 或对
  Agent 思维的逐步遥控。
- 这里的“稳定”指系统在超时、未知副作用、平台故障和恶意输入下仍保持状态分层、失败关闭和资源
  有界；不是数学意义上的闭环稳定性证明。
- 这里的“观察者/传感器”是验证权威，不是多 Agent 角色。引入名为 Observer 的编排角色仍属于
  被删除的认知脚手架。

本文与第一性原理的关系是：

```text
第一性原理：什么必须成立，谁能拥有什么权威
        |
        v
控制论：闭环如何构成，哪些信号能进入控制器，失败时如何收敛
        |
        v
架构与代码：模块、事件、检查点、验证器和测试
        |
        v
证据：E0-E4 等级、正反测试、故障注入、真实评测
```

## 2. 两层闭环

系统中存在两个互相约束、但不能合并的闭环；它们共享一条受控执行与证据脊。

```text
Agent 认知内环（Agent 拥有）
  observation/context
          |
          v
  Pi reasoning -> action selection -> tool/model call
          ^                                  |
          |                                  v
      observation <------------------- environment response

Harness 治理外环（Harness 拥有）
  reference/constraints
          |
          v
  admission -> lease -> budget -> dispatch -> actuator
          ^                                      |
          |                                      v
  control decision <-- verdict <-- evidence <-- independent sensor
          |
          +--> continue / finalize / deny / stop / recover
```

### 2.1 内环

- 由 Pi/Agent 负责观察、推理、规划、选择工具、读取知识和处理结果。
- Harness 通过 prompt、World Context、verifier feedback、budget 和显式 hint 提供输入，但不
  规定下一步认知动作。
- Agent 可以自由重试、反思、搜索或改变策略；这些行为不自动产生 evidence，也不自动改变目标。
- 内环的“完成”只是 Agent 不再请求工具或声明回合结束，不等于 objective verified。

### 2.2 外环

- 由 Harness 负责接纳、授权、隔离、记账、验证、恢复和终止。
- 外环只读取显式、可追溯的输入：deadline、预算、lease、operator cancel、objective verdict、
  unsafe unknown、平台/基础设施状态。
- 外环不做规划、技能排序、对子目标打分或给 Agent 注入启发式 steer。
- 外环的输出是 allow/deny、continue/finalize/stop、checkpoint/reconcile 和持久事实，不是
  对 Agent 思维质量的评价。

两层之间存在明确接口：Agent 事件与 submission 是 untrusted input；World Context 是有界事实
投影；verifier feedback 是授权反馈；budget/deadline 是外部边界；objective verdict 是唯一能
关闭目标闭环的信号。

## 3. 控制模型映射

| 控制概念 | Red Harness 对应物 | 所有权与边界 |
| --- | --- | --- |
| 参考值 / setpoint | Task objective、benchmark 目标和显式约束 | Task/Harness 设定；Agent 只能读取，不能改写。 |
| 控制器 | Pi 认知选择 + Harness 外部止损/恢复律 | 必须拆成两个控制器；Pi 不签发 verdict，Harness 不选择下一步任务动作。 |
| 执行器 / actuator | Pi 工具、模型调用、Gateway 和受控 broker | 仅在 lease、网络、预算和隔离边界内行动。 |
| 被控对象 / plant | 不可信目标环境、靶机、benchmark instance | 部分可观测、可能对抗、可能产生不可逆副作用。 |
| 传感器 / sensor | task verifier、docker verifier、benchmark evaluator | 独立产生 observation/evidence/verdict；不是 Agent 自评。 |
| 估计器 / estimator | World、Progress、Context、trace 投影 | 只从权威事实和有来源的 observation 派生；缺失必须披露。 |
| 误差 / feedback | objective status、action outcome、budget remaining、deadline | 不是 LLM 分数；budget/deadline 是边界，不是“进展”。 |
| 前馈/方向输入 | operator hint、task instructions、policy | 可改变探索方向，不能成为 verdict。 |
| 扰动 / disturbance | 目标噪声、平台限流、网络故障、恶意输入、模型波动 | 必须分类、记录和失败关闭，不得升级为成功。 |
| 死区时间 / dead time | 工具执行、远端副作用、unknown action、重启和验证延迟 | 在效果确认前不得把 dispatched/in-flight 当完成。 |

关键区别：

- **反馈不是自述。** Agent 的文字、置信度、计划完成和工具 exit code 是输入或 claim。
- **预算不是进展。** remaining budget 只决定是否继续或收敛，不证明任务更接近成功。
- **World 不是传感器。** World 是估计器/状态投影；它不能自行关闭 objective。
- **hint 不是反馈。** hint 是可信方向输入，不能替代 verifier/evaluator。

## 4. 控制不变量

C1–C10 是规范性要求。任何新增控制器、steer、stop、retry、cache 或恢复路径都必须说明它是否
影响这些不变量，并提供正反证据。

| ID | 不变量 | 违反后的典型症状 |
| --- | --- | --- |
| C1 | 参考值只能由 Task/Harness 设定，Agent 不得改写目标或约束。 | submission 内容、goal.completed 或 notes 改变目标定义。 |
| C2 | 只有授权 verifier/evaluator 能关闭 objective 闭环。 | Agent 自评、工具成功或 submission accepted 被当作完成。 |
| C3 | Claim、Observation、Evidence、Verdict 必须保持生产者与信任等级分层。 | 未引用 observation 的文本升级为 evidence 或 verdict。 |
| C4 | 控制输入必须显式、可观测、可追溯；重复、停滞、上下文长度等内部计数只作诊断。 | 启发式阈值触发 steer/stop，无法解释控制依据。 |
| C5 | 停止必须有外部边界，不能由 Harness 猜测何时应该结束。 | 无进展或多次失败后 Harness 自行宣告停止。 |
| C6 | dispatched/in-flight 不等于完成；unknown 副作用不得自动重放。 | 超时后重发动作，造成重复提交或重复破坏。 |
| C7 | World/Context 估计必须有界、有来源、可重建；知识缺失必须显式披露。 | 漂移投影被当作真相，或缺失信息被静默省略。 |
| C8 | Actuator 只能在 lease、网络、预算、凭据和隔离边界内行动。 | 容器写权威状态、越权网络、凭据进入 trace 或 worker。 |
| C9 | 平台故障、unavailable、基础设施扰动和恶意输出不得升级为成功。 | evaluator 不可用被记为成功，benchmark bug 触发自动重试风暴。 |
| C10 | Harness 不替 Agent 规划、排序、反思或选择动作。 | planner、DAG、技能排序、role choreography 重新进入 control spine。 |

## 5. 实现与证据矩阵

证据等级沿用 [first-principles.md](first-principles.md) 的 E0–E4。下表只记录当前仓库中能够复核
的证据；“未验证”不是缺陷豁免，而是下一次设计或评测的输入。

| ID | 当前强制点 | 现有证据 | 等级与缺口 |
| --- | --- | --- | --- |
| C1 | `src/harness/progress.py`、`src/harness/solver_loop.py` | `tests/test_control_properties.py`：Agent events 不能改写 active goal；progress updates 不能完成目标。 | E1；没有跨进程/跨会话的 setpoint 改写负面测试。 |
| C2 | `src/harness/verification.py`、`src/harness/runtime/evidence.py` | `tests/test_control_properties.py` 的 claim/tool exit/forged authority 测试；`tests/test_verification_evidence.py` 的不可用 evaluator 测试。 | E1；真实 verifier/evaluator 复跑仍待验证。 |
| C3 | `src/harness/session.py`、`src/harness/progress.py`、`src/harness/runtime/evidence.py` | `tests/test_control_properties.py` 的 unreferenced signal 与 in-flight action 测试；`tests/test_verification_evidence.py` 的 evidence 引用测试。 | E1；Observation 到 Action 的完整因果绑定仍待加强。 |
| C4 | `src/harness/solver_policy.py`、`src/harness/solver_loop.py`、`src/harness/pi_container.py` | `tests/test_control_properties.py` 的内部 stall counter 测试；`tests/test_solver_loop.py` 的 steer 白名单测试。 | E1；没有端到端验证所有环境下的控制输入来源。 |
| C5 | `src/harness/solver_policy.py` | `tests/test_control_properties.py`、`tests/test_control_law_properties.py`：稳态继续、零预算停止、非有限输入失败关闭。 | E1；真实运行中的 deadline/多预算组合仍需故障注入。 |
| C6 | `src/harness/kernel/action.py`、`src/harness/runtime/checkpoint.py` | `tests/test_kernel_action.py`、`tests/test_checkpoint.py`：重复接纳、未知动作、重开、显式对账、旧 lease 拒绝。 | E1/E2（局部故障注入）；缺 Docker-backed 五窗口崩溃矩阵。 |
| C7 | `src/harness/world/`、`src/harness/world/context.py` | `tests/test_world_context.py`、`tests/test_sqlite_world.py`：确定性投影、来源、边界和 replay。 | E1/E2 局部；World/Context/Progress/Checkpoint/Result 的账本重建未完成。 |
| C8 | `src/harness/kernel/action.py`、`src/harness/pi_container.py`、`src/harness/gateway.py` | `tests/test_architecture.py`、`tests/test_container_boundary.py`、`tests/test_network_policy.py`、`tests/test_kernel_action.py`。 | E0/E1；Docker/Pi 真实环境路径依赖本机能力，CI 之外仍需显式运行。 |
| C9 | `src/harness/runtime/evidence.py`、`src/harness/runtime/lifecycle.py` | `tests/test_verification_evidence.py`、`tests/test_checkpoint.py` 的 unavailable/失败关闭路径。 | E1；没有真实 TSec sweep 和 evaluator 复跑证据。 |
| C10 | `src/harness/solver_policy.py`、`src/harness/solver_loop.py`、`src/harness/pi_container.py` | `tests/test_architecture.py` 的 DELETE/CONTROL_SPINE 防线；`tests/test_solver_loop.py` 的 steer 白名单。 | E0/E1；架构边界有测试，认知层是否被模型正确使用仍需真实行为评测。 |

### 5.1 证据等级的含义

- **E0**：静态检查、lint、import/architecture 边界。能证明形状，不证明行为。
- **E1**：单元测试、负面测试。能证明局部契约，不证明系统集成。
- **E2**：集成测试、故障注入、replay。能证明生命周期和恢复路径，不证明真实任务效果。
- **E3**：真实 benchmark、多 trial、trace 复核。能证明目标范围内可用，不证明跨域泛化。
- **E4**：持续 sweep、生产观测、回归对照。能证明稳定操作价值，不证明永恒正确。

## 6. 当前架构缺口

以下是规范性目标与当前证据之间的主要距离。它们不改变 roadmap 状态，也不表示对应能力在所有
路径上不存在。

1. **观察—行动因果绑定未完全闭合。** Action admission、lease 和 unknown reconciliation 已有
   局部强制点，但 v2 读路径仍需要证明每个跨边界动作都能沿 Run -> Attempt -> Action ->
   Observation -> Evidence -> Verdict 追溯。
2. **崩溃矩阵仍不充分。** checkpoint 和 kernel 的单元故障注入不能替代 Docker-backed 五窗口
   dispatch/effect/feedback/checkpoint 测试；真实进程和容器信号仍有未覆盖区间。
3. **账本重建仍是目标而非生产事实。** v3 causal ledger 当前是 shadow，World/Context/Progress/
   Result/Checkpoint 仍以 v2 为权威读路径；删除投影并从账本重建尚未完成。
4. **真实平台效果未证明。** TSec sweep、最终 evaluator 复跑、多 trial 方差和基础设施噪声仍未
   闭合；因此不能用本地测试全绿替代 agent 效果提升。
5. **系统级控制稳定性缺少量化。** 当前证据主要覆盖逻辑不变量，没有统一的反馈延迟、unknown
   持续时间、恢复成功率和无效 steer 指标；不能声称闭环在真实运行中“稳定”。

## 7. 从来源提炼的控制原则

来源卡与版本在 [first-principles.md](first-principles.md) 第 1.4 节。这里只记录被本项目采用或
拒绝的原则，不复制外部架构。

### 7.1 采用

- **A1 + A4：** 先用最简单的可观测机制；每个 harness 组件都是关于模型不足的假设，必须有收益、
  成本和删除条件。
- **A2 + A3：** context 是有限资源；长任务靠有界投影、进度工件、checkpoint 和交接，而不是无限
  累积历史。
- **A5 + O1 + O3：** 环境、意图、反馈循环和可复核的 outcome/transcript 是工程对象；稳定协议和
  生命周期必须为证据服务。
- **A6 + O4：** 执行边界必须由运行时/OS 强制，不能只依赖 prompt、审批文案或 Agent 自觉。
- **O2：** Agent 的内环终止只是回合结束；它不能关闭 objective 外环。
- **G1：** 在需要确定性授权的分类门上，LLM 只提交类型化特征，Python 组合允许/拒绝；LLM 分数、
  自评和 majority vote 不能成为目标 verdict。

### 7.2 拒绝或归 Agent 侧

- 拒绝把 planner、DAG、技能排序、黑板、角色编排或自治 worker 队列重新放进 Harness。
- 拒绝用 LLM-as-scorer、复合质量分或 reward 驱动 Agent 的下一步探索。
- 拒绝把多 Agent 讨论、Reflexion、搜索树和记忆蒸馏自动升级为 evidence；这些属于 Agent 侧策略，
  只能留下 claim/observation。
- 拒绝把外部文章里的“高成功率”或“零人工代码”当成 Red Harness 的验收标准；本项目首先要求安全、
  可审计、可恢复，不能以速度替代信任边界。

## 8. 设计评审清单

任何控制相关变更是“增加闭环”还是“增加开放回路”，必须回答：

1. 参考值和约束由谁设定，Agent 能否间接改写？
2. 独立传感器是谁，什么事件能产生 evidence，谁有权签发 verdict？
3. 控制器读取哪些输入，哪些只是诊断？是否会产生不可解释的 steer/stop？
4. actuator 的 lease、网络、凭据、预算和副作用边界在哪里？
5. 估计器从哪些事实派生，缺失、冲突和延迟如何表示？
6. 正常、超时、崩溃、unknown、重复调用、平台不可用时分别如何收敛？
7. 它属于 Agent 内环还是 Harness 外环？是否违反 C1–C10 中的任何一条？
8. 正向测试、负面测试、故障注入和真实评测证据分别是什么？
9. 模型或平台升级后，如何 ablation、shadow 或删除它？

## 9. 结论

控制论不是让 Harness 接管 Agent 的推理，而是让 Agent 的自主性发生在可授权、可观测、可恢复、
可验证的边界内。

> **Agent 可以选择下一步；Harness 只能决定这一步是否被允许、发生了什么、何时必须停下，以及
> 什么证据足以改变目标结论。**
