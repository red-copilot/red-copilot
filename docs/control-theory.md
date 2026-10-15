# 控制论驱动的 Harness 设计与精简

基线：`main` @ `62485bc`（2026-10-13）。本文用控制论（Wiener）与工程控制论（钱学森）
重述 Red Harness 的架构，并据此给出一版"控制主脊"内核。目标是让一个面向 TSec
benchmark 的自主安全智能体，在**最小**的闭环反馈结构上工作，而不是在层层叠加的框架里工作。

需要强调：本文不是要引入新的控制论框架，而是把已有模块重新解释为一条闭环，并删掉
不在这条闭环上的复杂度。术语只用于统一认知，落地仍是现有模块（`solver_loop.py`、
`solver_policy.py`、`planner.py`、`verification.py`、`world/`、`runtime/`）。

## 一、控制论视角

### 1.1 基本映射

控制论的核心是"带反馈的循环因果系统"。把一次任务求解看成对一个被控过程的调节，
Harness 的每个部件都能落到标准控制回路中的一个位置：

| 控制论概念 | Harness 对应 | 说明 |
| --- | --- | --- |
| 参考 / 设定值 r | `TaskSpec.objective`、TSec 题目目标与剩余 flag | 系统希望达到的状态 |
| 被控对象 P | 授权范围内的目标环境 / 靶机 | 动作作用于其上 |
| 执行器 | Pi 的工具调用（`bash`/`edit`/`read`/`write`/网络） | 把控制量施加到被控对象 |
| 传感器 | `verifier.py`、`benchmark` evaluator、`verification.py` | 唯一有权威的测量通道 |
| 估计器 | `world/`（事件溯源状态 + 快照） | 从带噪观测中估计当前状态 |
| 控制器 C | `solver_policy.py`（纯控制律）+ `planner.py`（滚动时域）+ Pi（内层推理） | 依据误差选择行动 |
| 误差 e | 目标与已验证状态之差（r − ŷ） | 驱动的动力来源 |
| 扰动 d | 目标防御、错误假设、工具/模型故障、环境噪声 | 必须被反馈抑制 |
| 约束 / 饱和 | `budget.py`（token、调用、时间、成本） | 执行器与回路的物理限幅 |
| 状态持久化 | `runtime/checkpoint.py`、`progress.py` | 断电/崩溃后的回路状态 |

关键结论与现有信任边界一致：**只有传感器（验证器）可以闭合反馈环**。Agent 的自述、
工具退出码、容器输出都是"观测"，在成为可信测量前只是 claim，不构成误差信号。

### 1.2 工程控制论要点

钱学森《工程控制论》关心的是"如何在扰动下设计出满足指标的控制系统"。对应到本题：

- **闭环 vs 开环**：只有把验证结果回注到下一步推理，系统才具备抗扰能力。开环地"多执行
  几步再说"不会提高成功率，只会消耗预算。
- **级联（双回路）控制**：外层是 supervisory 控制（Harness 设定子目标、纠偏、限幅、
  停机），内层是 Pi 的推理与工具使用。外环不替 Pi 做每一步推理，只调节设定值和约束。
- **滚动时域控制（MPC）**：`RollingHorizonPlanner` 视界 1–3，等价于 receding-horizon
  control：每步只解一小段最优问题，执行一步，观测后再解。**不**升级成全局攻击 DAG。
- **可观测性**：只有能被独立测量的量才能作为反馈。无法独立测得的"成功"不可观测，不能
  进入闭环——这正是 VER 信任边界。
- **可控性**：只有其效果可被观测/确认的动作才允许作为控制量；会留下不可观测副作用的
  动作需要先对账（reconciliation）再重试。
- **稳定性与抗振荡**：无限重规划或重复同一决策是回路发散/振荡。用限幅（重规划上限）、
  阻尼（无进展停机、预算阈值）保证收敛。
- **时滞与采样**：验证有延迟，属于控制回路的 dead time。采样点放在事件边界（每个
  session 事件后写 checkpoint），避免在未确认效果时推进状态。
- **必要多样性（Ashby）**：控制器的多样性要覆盖被控对象的多样性。做法是扩充**工具与
  世界上下文**，而不是堆叠框架层——后者只增加内部多样性，并不增加对目标的控制能力。

## 二、单条控制主脊

主脊是一条闭环，其它一切都是它的插件点：

```
        reference r (objective / remaining flags)
                 |
                 v
   +--> error e = r - y_hat            (solver_policy.decide)
   |            |
   |            v
   |     controller C                    (planner 滚动时域 + Pi 内层推理)
   |            |
   |            v
   |     actuator                        (Pi tools: bash/edit/read/write/network)
   |            |
   |            v
   |     plant P                         (authorized target / challenge)
   |            |
   |            v
   |     sensor                          (verifier / benchmark evaluator)
   |            |
   |            v
   |     estimator --------> y_hat       (world/ state + evidence)
   |            |
   +------------+  feedback (verified evidence only)
```

- **估计器 = `world/`**：事件溯源状态、快照与检索，是控制器看到的"当前状态估计"。
- **传感器 = `verification.py` + `action_verifier.py` + `verifier.py`**：只接受带证据引用、
  生产者身份与世界 revision 的测量；产出 `ActionVerdict` / `ObjectiveVerdict`。
- **控制器 = `solver_policy.py` + `planner.py` + Pi 推理**：纯决策与副作用分离（`RUN-01`），
  决策只依据目标、世界 revision、已证/待证证据、失败与剩余预算。
- **执行器 = `agent.py` / `session.py` / `pi_adapter.py` / `tool_adapter.py` / `gateway.py`**。
- **约束 = `budget.py`**：回路软饱和；阈值先触发收敛引导，越限后 finalize 写摘要再终止，
  而非静默超支或硬截断。
- **状态持久化 = `runtime/`**：`checkpoint.py`、`progress.py`、`lifecycle.py`。

## 三、精简判据与裁剪

### 3.1 判据

不属于上述闭环、且不提供权威反馈的子系统，即为"开环复杂度"。它们可以在离线、分布式或
评估场景中有用，但对"单智能体闭环求解一个 TSec 题目"没有贡献，且扩大信任面与维护面。
按此判据删除。

### 3.2 删除清单（开环复杂度）

| 子系统 | 文件 | 为什么不在主脊 |
| --- | --- | --- |
| 控制平面 HTTP | `api/`、`control_plane.py` | 分布式调度/观测入口，不参与单次闭环 |
| 作业队列 | `queue.py` | 多机分发的开环调度 |
| Worker | `worker.py` | 租约/心跳属于分布式执行，非单回路 |
| 协调黑板 | `coordination.py` | 多智能体协商，未证明收益 |
| OTLP 导出 | `otel.py` | 离线遥测导出，非反馈通道 |
| 排行/评分 | `scoring.py` | 跨运行聚合，与单题闭环无关 |
| 套件并行 | `suite.py` | 多运行评估入口；TSec 由 `--all` 覆盖 |
| 评估对比 | `evaluation.py` | 离线 A/B 报告，非在线控制 |
| 基准注册表 | `registry.py` | 仅服务于控制平面的发现接口 |

CLI 命令随之移除：`serve`、`submit`、`worker`、`otel-export`、`suite`、
`validate-suite`、`compare-evaluations`。保留：`run`、`tsec`、`validate`、`preflight`、
`validate-skill`、`gateway`、`capabilities`、`audit-run`、`report`。

### 3.3 保留（主脊）

`solver_loop.py`、`solver_policy.py`、`planner.py`、`skills.py`、`progress.py`、
`verification.py`、`action_verifier.py`、`verifier.py`、`world/`、`runtime/`、
`benchmark/`、`agent.py`、`session.py`、`pi_adapter.py`、`pi_container.py`、
`tool_adapter.py`、`gateway.py`、`tool_plugins.py`、`budget.py`、`orchestrator.py`、
`trace.py`、`audit.py`、`preflight.py`、`execution.py`、`failures.py`、`telemetry.py`、
`process_guard.py`、`models.py`、`policy.py`、`secureio.py`、`environment.py`、
`network_policy.py`。

这些模块构成导入闭包受限的"控制主脊"：从 `runtime.bootstrap`、`benchmark.runner`、
`solver_loop` 出发的导入闭包不得包含任何被删子系统，且模块数有上限（由测试冻结）。任何
想扩大的改动都必须先证明它提升了闭环的控制能力。

## 四、稳定裕度在代码中的落点

控制律本身是纯函数（`solver_policy.decide_solver_action`），稳定裕度由以下机制保证：

- **限幅**：默认重规划次数上限（`DEFAULT_MAX_REPLANS = 8`，外部 1 小时基准评测模式放宽至
  `BENCHMARK_MAX_REPLANS = 16`）——避免控制量无界振荡。
- **阻尼 / 死区**：连续无进展上限（6）与剩余预算阈值（5%）触发 STOP——避免在死区里反复
  消耗预算。
- **中性执行不触发停机**：一次成功的工具执行既不是已确认进展，也不是停滞。它只累加
  `unconfirmed_progress`（本地默认上限 `40`，TSec 基准评测模式默认上限
  `BENCHMARK_MAX_UNCONFIRMED_PROGRESS = 160`），只有失败/矛盾/被拒提交才累加
  `no_progress_count`（上限 6）。这样在缺少动作级验证器的 benchmark 路径（例如 TSec 只对
  提交的 flag 判分），既不会在探索 6 步后被误判为无进展，也不会在 1 小时长题的深入分析中因
  40 次工具调用而过早停机。
- **初始可达性能力派生**：`bootstrap_solver()` 在注入 `benchmark.target` 实体时，以相同的
  `target_actor`（`evidence` 级可信度）自动派生 `network.reachability` 能力，确保
  `RollingHorizonPlanner` 在开局即可激活匹配的领域技能。
- **防重复**：`ProgressLedger` 用状态签名抑制相同重规划，避免稳态振荡。
- **饱和**：`budget.py` 对 token/调用/成本/时间维度做限幅，且全部为**软饱和**：任一维度
  越限先触发 finalize（写摘要、停止开新探测），只有在宽限窗口内仍未收敛才终止，避免
  硬截断丢失已获得的证据。
- **采样/时滞**：每个 session 事件后写 checkpoint，崩溃恢复前要求未决动作对账，保证
  回路不会在效果未知时继续推进。
- **可观测反馈**：`SolverLoop` 只把带证据引用的裁决并入反馈；无引用的信号降级为 claim。

## 五、TSec 参赛智能体设计

参赛智能体的主体是"Pi 作为被控推理器 + Harness 作为控制器"：

- **参考**：TSec 题目目标与剩余 flag 数（`challenge_total_score`、`remaining_at_start`）。
- **传感器**：平台 evaluator（`SubmitResult.awarded`）与本地验证器；Harness 独占
  `BENCHMARK_TOKEN`，Pi 容器从不接触。
- **控制器**：外层 Harness 依误差决定 continue/steer/replan/verify/stop 并逐轮注入反馈；
  内层 Pi 用工具完成具体动作。
- **执行器**：Pi 在 Kali 容器内的工具集（`read`/`bash`/`edit`/`write`，按需 `NET_RAW`）。
- **提交协议**：Pi 向 `$HARNESS_SUBMISSION_INBOX` 追加 JSON 候选（或以 `HARNESS_FLAG=flag{...}`
  兜底），Harness 经 SDK 提交并记录 flag 的 SHA256（不落明文）。

级联结构的关键约束：Harness **不**替 Pi 完成每一步推理，只调节设定值（子目标）、约束
（预算/网络/权限）与停机条件；Pi **不**能把自己的结论升级为已验证事实。

### 消融方案（同一模型、任务、种子、预算）

`harness tsec` 与 `harness run` 均支持 `--solver-profile` 参数直接切换实验配置：

| 实验 | `--solver-profile` | 目标 |
| --- | --- | --- |
| Pi 原生求解 + 闭环验证反馈 | `pi-only` | 基线 + 闭环反馈收益 |
| Pi + World 上下文 | `pi-world` | 度量持久状态与 Focus 集收益 |
| 全量控制主脊（含 Rolling Planner） | `pi-world-heuristic` | 度量技能选择与总体效果 |

## 六、验收：可证伪的控制性质

精简不能只看目录变短。以下性质由 `tests/test_control_properties.py` 断言：

1. **反馈权威**：Agent 自述或工具退出码不能置参考达成，只有权威 `ObjectiveVerdict` 能。
2. **稳定 / 无发散**：扰动事件流下重规划有上限、无重复同决策振荡、最终收敛到 STOP。
3. **参考不可篡改**：Agent 输出不能改写目标与剩余范围。
4. **可观测性**：无证据引用的信号只记为 claim，不进入反馈。
5. **反事实决策**：世界状态改变时下一步决策随之改变（保留 `PLN-01`）。
6. **时滞 / 恢复**：checkpoint 崩溃窗口不重放已产生的副作用（保留 `RUN-02`）。

外加规模度量：包模块数与 `src` 行数下降，且 `run`/`tsec` 的导入闭包不再包含被删子系统。

## 七、不做什么

- 不引入神经世界模型、微服务、向量数据库或新的"控制框架"层。
- 不重命名 `solver_loop`/`solver_policy`，不建 `control/` 包——控制主脊由现有模块承担，
  避免"以抽象换简化"。
- 不改 World 事件格式与 `verification.py` 的证据/裁决契约；信任边界保持不变。
