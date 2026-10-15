# 智能体工程设计：目标驱动的意图-事实前沿

本文描述 Red Harness 的**智能体认知与决策闭环**：Agent 如何在 World 状态、
技能元数据和显式目标之间选择下一步动作。设计保持确定性，并把"前沿选择"与
"候选打分"分开，使该层可以在没有模型/凭据的环境中被完整测试。

落地模块：`src/harness/cognition.py`（意图-事实投影与前沿规划）、
`src/harness/planner.py`（滚动时域封装与启发式打分）、
`src/harness/runtime/bootstrap.py`（按 profile 组装）、
`src/harness/solver_loop.py`（把前沿投影写进 Agent 上下文）、
`src/harness/extract.py`（工具输出的事实抽取，见 §8）。

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

## 7. 技能目录对智能体可见

### 7.1 问题

技能元数据（`SkillSpec` 的 `requires`/`produces`）此前只服务 Harness 侧规划：
`frontier_context()` 与 `round_brief()` 只渲染意图 id 与描述，Agent 看不到

- 每个前沿意图**声明的状态转移语义**（消费什么事实、产出什么事实）；
- 距离可执行**只差一步的技能**：它的哪个 `requires` 尚未满足，即"再建立
  什么事实就能解锁什么能力"。

结果是 Agent 只能按 id 盲目选择意图，无法主动补前置。

### 7.2 设计

- 前沿行升级为目录条目：`- [goal] <skill-id> (depth=n): <描述>` 之后追加紧凑
  选择器记法 `requires <kind>:<type>[...] -> produces <kind>:<type>[...]`，
  有多个选择器时按声明顺序以 `,` 连接。
- `IntentFactDAG.next_unlocks()`：后向链展开（§4 第 3 步）在沿未满足
  `requires` 深入时本来就会**经过** pending 意图；把这些意图连同其未满足的
  `requires` 选择器与深度收集为 `UnlockHint`，确定性排序（深度、id），渲染为
  `### Next unlocks` 段：`- <skill-id> (depth=n): needs <kind>:<type>, ...`。
- 接线点不变：`frontier_context()` 写入 `world.context.txt`，`round_brief()`
  的 frontier 段同步携带选择器后缀——目录在两个 Agent 可见通道上保持一致。

### 7.3 边界

- 纯元数据投影：技能目录不含任何可执行内容（`SkillSpec` 本身就无命令字段），
  Pi 自身 skills/extensions 保持关闭，不因目录可见而开启。
- 信任边界不变：目录影响动作选择，不影响目标达成判定；`claim` 级记录依旧
  不能满足 `minimum_trust=evidence/verified` 的前置。
- 有界且确定：沿用 `max_chars` 截断与既有排序，同一 snapshot + skills 始终
  渲染出同一目录。

## 8. 回合质量：事实抽取扩展

### 8.1 问题

`graph_changed` 回合依赖事实面增长，但首轮落地只解析 nmap/gobuster/dirb/ffuf
四种格式；实测运行里 Agent 主要使用 `curl | head`/`cat`，事实面不增长，回合
大多以 `stall` 兜底触发（PLN-06 遗留）。

### 8.2 设计

扩展 `extract.py` 的确定性抽取，覆盖 Agent 实际使用的工具与 Kali 镜像中
高信号的固定格式：

| 工具 | 特征行（正则锚点） | 产出事实 |
| --- | --- | --- |
| `curl -v` | `> GET /path HTTP/1.1` 与紧随的 `< HTTP/1.1 200 OK` 配对 | `web.endpoint {path, status}` |
| `whatweb` | `http://t [200 OK] Apache[2.4.41]` | `web.technology {url, components}` |
| `hydra` | `[22][ssh] host: h   login: u   password: p` | `credential.access {service, host, login, password}` |
| `sqlmap` | `parameter 'id' is 'AND boolean-based blind ...' injectable` | `web.vulnerability {parameter, technique}` |
| `nikto` | `+ /admin/: Admin login page found` | `web.endpoint {path, note}` |

`curl -v` 的配对语义是保守性的关键：裸文本中的 `HTTP/1.1 200`（无请求行
上下文）、`curl -i` 的纯响应头、普通散文都不产出事实。

### 8.3 原则（与首轮一致）

只匹配高区分度正则；逐结果上限 `MAX_EXTRACTED_FACTS=25`；内容寻址 id 去重；
产物是 Harness 观测的 `evidence` 级 observation（source=`harness.extract`），
不携带原始输出；同一段输出重复出现不重复入图。

## 9. 非目标

- 不引入学习式检索或模型规划作为主脊；`Planner` 协议为其预留插件点。
- 不新增世界记录类型承载"意图"，也不把阶段/权重等评测语义写入通用闭环。
- 不把前沿升级为全局攻击 DAG：它仍是滚动时域（视界 1–3）的局部工作集。
- 不做通用输出解析器：抽取只认固定格式，不猜测语义，也不用模型解析工具输出。
