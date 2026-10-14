# 智能体工程设计：目标驱动的意图-事实前沿

本文描述 Red Harness 的**智能体认知与决策闭环**：Agent 如何在 World 状态、
技能元数据和显式目标之间选择下一步动作。设计保持确定性，并把"前沿选择"与
"候选打分"分开，使该层可以在没有模型/凭据的环境中被完整测试。

落地模块：`src/harness/cognition.py`（意图-事实投影与前沿规划）、
`src/harness/planner.py`（滚动时域封装与启发式打分）、
`src/harness/runtime/bootstrap.py`（按 profile 组装）、
`src/harness/solver_loop.py`（把前沿投影写进 Agent 上下文）。

## 1. 现状诊断

改造前，仓库里已经存在一份"意图-事实 DAG"投影（原 `offensive.py`），但它：

- **未接线**：`bootstrap_solver` 固定使用 `RollingHorizonPlanner` 加
  `HeuristicSkillPlanner`；相位前沿规划器只在单元测试里被调用。
- **绑定场景**：阶段枚举 `RECON/DISCOVERY/EXPLOIT/REPORT` 及其权重直接来自
  某个众测平台的自主度评分，属于评测语义，不应进入通用闭环。
- **Agent 看不到**：写入 `world.context.txt` 的是 `WorldContextBuilder` 输出，
  并不是 DAG 投影，"可执行前沿"只存在于文档和提示词里。

因此本次改造的目标是：把该投影改造成**平台无关、纯目标驱动**的前沿，并真正
接到求解闭环与 Agent 上下文上。

## 2. 目标驱动的意图-事实闭环

三方输入推导出"下一步可执行前沿"：

| 输入 | 来源 | 含义 |
| --- | --- | --- |
| 事实（fact） | `WorldSnapshot` 各类记录 | 当前已被观测/验证的状态 |
| 意图（intent） | `SkillSpec` 元数据 | 一个技能可执行时产生的状态转移 |
| 目标（goal） | `Goal.required_states` | 期望达到的目标状态选择器 |

三者构成二部图：事实满足意图的 `requires`，意图的 `produces` 产生新事实。
**前沿**只包含"前置条件已满足"（`enabled`）且（传递地）推进某个未达成目标的
意图。没有任何目标声明选择器时，前沿退化为全部 `enabled` 意图，保持既有行为。

意图是**派生投影**：不落盘、不新增 `WorldObjectKind`、不改事件 schema。同一个
snapshot 与 skills 始终得到同一个前沿。

## 3. 接口与数据流

- `Goal` 新增可选字段 `required_states: list[StateSelector]`；`ObjectiveSpec`
  同样新增 `required_states`，在 `orchestrator.py` 与 `benchmark/runner.py`
  构造 root goal 时映射过去。缺省为空 → 旧任务行为不变。
- `StateSelector` 上提到 `world/models.py`，由 `skills.py` 重导出，
  `from harness.skills import StateSelector` 等既有导入路径保持可用。
- 数据流：

```text
TaskSpec.objective.required_states
        │  (orchestrator / benchmark runner)
        v
Goal.required_states ──┐
WorldSnapshot (facts) ─┼─> IntentFactDAG.from_snapshot ──> frontier()
SkillSpec[] (intents) ─┘                                     │
                                                             v
                     GoalFrontierPlanner.propose ──> PlanCandidate[]
                                                             │
                        RollingHorizonPlanner ──> plan.json / trace
                                                             │
                     frontier_context() ──> world.context.txt（Agent 可见）
```

## 4. 前沿算法

`IntentFactDAG.frontier()`：

1. **目标种子**：取 `active`/`pending` 目标中尚未被满足的 `required_states`。
   满足判定复用 `_selector_matches`，即记录必须有效且达到 `minimum_trust`。
2. **直接命中**：某意图的 `produces` 覆盖某个未达成目标选择器，即为目标直击
   （`goal_hit=True`，`depth=0`）。
3. **后向链前置**：若直击意图尚未 `enabled`，其未满足的 `requires` 作为深度 +1
   的新种子，沿 `requires→produces` 反向链补前置，直到 `MAX_FRONTIER_DEPTH=3`。
   环由深度上限切断。
4. **只暴露可执行项**：前沿仅包含 `enabled` 的意图；未就绪的中间意图只用于展开
   前置，不作为候选。
5. **确定性排序**：目标直击优先，其次深度，最后 `skill_id` 兜底。
6. **活性兜底**：无目标选择器、或目标没有任何技能可覆盖时，退化为全部
   `enabled` 意图，避免"没有候选"的死循环。

`GoalFrontierPlanner`（`name="goal-frontier"`）实现既有 `Planner` 协议：先取
`HeuristicSkillPlanner` 的确定性得分作为基础项，裁剪到前沿，再叠加直击/深度加成。
启发式因此降级为**评分项与消融基线**，而不再是唯一的候选来源。

## 5. 上下文与信任边界

- `SolverLoop` 与 `bootstrap_solver` 在写 `world.context.txt` 时，按
  "focus 块 + 前沿块"组合；前沿块由 `cognition.frontier_context()` 生成并受字符
  预算约束。组合发生在控制主脊，`world/` 不反向依赖 `cognition`。
- 前沿只影响**动作选择**，不改变信任边界：目标达成仍由权威验证器
  （`AuthorizedObjectiveVerdict`）判定；Agent 自述与工具退出码仍是观测而非证据，
  `claim` 级记录不能冒充 `evidence`/`verified` 目标状态。

## 6. 运行时 profile 与消融

`--solver-profile` 现有四档：

| profile | World 上下文 | 候选规划器 |
| --- | --- | --- |
| `pi-only` | 关 | 无 |
| `pi-world` | 开 | 无 |
| `pi-world-heuristic` | 开 | `HeuristicSkillPlanner` |
| `pi-world-goal`（默认） | 开 | `GoalFrontierPlanner` |

`pi-world-heuristic` 保留为**非前沿消融基线**：固定种子下对比两档 profile 的按域
完成率与成本，即可衡量前沿裁剪是否带来收益。该对比需要 Docker/Pi/模型凭据，属于
环境依赖项；确定性 fixtures 覆盖前沿演进、后向链、信任级别、深度上限与环安全，
在无凭据环境即可完整回归。

## 7. 非目标

- 不引入学习式检索或模型规划作为主脊；`Planner` 协议为其预留插件点。
- 不新增世界记录类型承载"意图"，也不把阶段/权重等评测语义写入通用闭环。
- 不把前沿升级为全局攻击 DAG：它仍是滚动时域（视界 1–3）的局部工作集。
