# 赛场改进设计：从获奖开源代码倒推的能力补齐

> 依据：[zc-architecture-comparison.md](zc-architecture-comparison.md)（两届获奖队伍开源代码的代码级对比）。
> 定位：把对比得出的差距写成可实现的方案，目标是"下一届能不能多拿分"，不是把工程质量再抬一档。
> 边界：本文只做设计，不改代码、不新增模块；实现按 A → B → C 分阶段另开 PR。
> C 段涉及执行边界，必须先走 maintainer 复核。系统设计的非目标（Harness 侧规划、向量检索、神经世界模型）
> 依然有效，见 [agent-architecture-v2.md](agent-architecture-v2.md)。

## 0. 分期与提分排序

| 阶段 | 项目 | 落点 | 提分影响 | 改执行边界 |
| --- | --- | --- | --- | --- |
| A1 | 墙钟上限解除（ZCP-11） | `src/harness/benchmark/tsec_adapter.py`、`src/harness/cli.py` | 高（直接把可用赛窗还给 Agent） | 否 |
| A2 | 上下文重启与接力简报（ZCP-05 阶段一） | `src/harness/solver_loop.py`、`src/harness/benchmark/runner.py` | 高（对抗长轮次认知退化） | 否 |
| A3 | 重复动作打断（ZCP-06） | `src/harness/progress.py`、`src/harness/solver_loop.py` | 高（把卡死的一小时救回来） | 否 |
| A4 | 知识库填充（ZCP-07） | `skills/offensive/`、`knowledge/`、新增取数脚本 | 高（"不知道 payload 怎么写"是主要丢分点） | 否 |
| B1 | 失败路径看板（ZCP-08） | `src/harness/world/context.py` | 中 | 否 |
| B2 | 战绩归因（ZCP-09） | `src/harness/benchmark/runner.py`、`src/harness/cli.py` | 中（选型靠它，不直接得分） | 否 |
| B3 | 收尾加固（ZCP-10） | `src/harness/solver_loop.py`、`src/harness/cli.py` | 中（把已得分的东西写下来） | 否 |
| C | 容器内 Pi 扩展（ZCP-05 阶段二） | 运行期 agent 目录 + `docs/security/execution-boundary.md` | 高（对齐获奖方案的大输出落盘与压缩钩子） | **是** |

排期口径：A1 独立且最小，先合并；A2 与 A4 决定"能不能打出来"；A3 决定"打偏后能不能拉回"；
B 段随 A 段之后；C 段在 maintainer 复核通过前不动。

落地状态：A1–A4 与 B1–B3 已实现；C 段已获 maintainer 复核并按默认关闭落地
（`pi.harness_extension`），证据见 [agent-engineering.md](agent-engineering.md) 的 ZCP 表。

## 1. 现状核对：先纠正对比文档里的四处判断

设计前把所有落点在代码里核了一遍，四处结论需要修正，避免按错误前提排活。

1. **墙钟 3600s 是代码缺陷，不是赛制问题。** `src/harness/benchmark/tsec_adapter.py` 第 338 行把有效预算写成
   `min(budgets.wall_time, 3600)`。这条路径上"声明的预算"来自 `harness tsec --wall-time`（默认 3600；
   `benchmarks/examples/zc-hackathon/task.yaml` 的 7200s 只作用于 `harness run`），所以传 7200 也会被静默腰斩。
   容器硬截止是 `wall_time + FINALIZE_GRACE_SECONDS`（`src/harness/pi_container.py` 第 433 行、
   `src/harness/budget.py` 第 23 行的 180s），只要改上层预算这一处，下游自动一致。
2. **`args_family` 已经产出，只缺消费者。** `src/harness/pi_adapter.py` 第 33 行的 `_command_family` 已经把
   "同一条命令换个 wordlist 再跑一遍"归成同一族，并在第 195-196 行随 `tool.call` 事件发出
   `args_fingerprint` 与 `args_family`。循环里没有任何一处读取它，所以 A3 是"加消费者"，不是"加埋点"。
3. **refuted 假设是在投影层被过滤掉的。** `src/harness/world/context.py` 第 261-263 行渲染 "Open hypotheses"
   时只保留 `open` 与 `supported`，被证伪与被取代的路径对 Agent 完全不可见；`Failures` 段（第 353 行）已经存在。
   所以 B1 是改一个过滤加一段有界渲染，不是新建板块。
4. **收尾链路已经存在，B3 是加固。** 软限触发 `solver.finalize`（`src/harness/solver_loop.py` 第 448 行）、
   宽限期内挂住 STOP（第 492 行）、Agent 静默未写摘要时补问（第 509 行）都已实现。缺的是结构化要求与可观测性，
   不需要再造一个收尾会话。

## 2. A 段设计

### A1 墙钟上限解除（ZCP-11）

- 目标：任务声明的 `budgets.wall_time` 就是有效赛窗，Harness 不再偷偷打折。
- 设计：删除第 338 行的 `min(..., 3600)`，`effective_budgets` 直接沿用声明值；未声明时仍走 `BudgetSpec` 默认 3600。
  新增运维安全阀 `--max-wall-time-seconds`（可选，默认不设上限），用于防止误声明把机器占满。
- 接口：`harness tsec` 去掉隐式截断，新增一个可选上限参数；容器硬截止与宽限期不变。
- 失败模式：三题并行时单题赛窗变长会同时占住三个实例与三份机器容量，安全阀与 `--parallel` 需要一起看。
- 验收：单题有效预算等于声明值（含 zc-hackathon 的 7200s）；显式传安全阀时上限生效；未声明预算仍回落默认值。
- 实现说明：默认值保持 3600，只有在显式传入更大的 `--wall-time` 时才真正延长；证据见
  `tests/test_tsec_adapter.py`。

### A2 上下文重启与接力简报（ZCP-05 阶段一）

- 目标：在长轮次退化之前主动重置会话上下文，把"已确立事实、已排除路径、开放意图"以简报形式回注，
  对齐获奖方案的状态重置 + 记忆回注，但不引入容器内钩子。
- 触发：自上次重启起工具调用达到 300 次，或自上次重启起又用掉声明的 `max_tool_calls` 的 60%，
  先到先触发（两个阈值都以"上次重启"为窗口，否则刚跨过的阈值会在下一次事件上立刻再触发一次）；只在
  一道题内触发，且每题默认最多重启 2 次（避免把赛窗花在重置上）。
- 接力简报：写入会话根目录的 `carry-forward.md`，字符上限 8000，内容固定四段——已确立事实（带证据引用）、
  已排除路径、开放意图、指针（笔记目录、`world.context.txt`、上一轮摘要文件）。简报只读，不含凭据、不写权威工件。
- 动作序列（阶段一已落地）：结束旧会话并清掉它留下的容器，以**新 session id** 重建容器后启动新会话——
  容器名由运行目录派生，工作区与笔记目录的挂载不变，换掉的只有模型会话；新会话以简报作为起始 objective
  注入，旧会话产物归档为 `restart-N` 副本供审计。原计划的"先 steer 要求 Agent 把结论写回
  `HARNESS_NOTES_DIR`"不在阶段一：简报由 Harness 从世界投影生成，不依赖 Agent 先沉淀笔记；需要 Agent
  侧配合的沉淀留给阶段二（容器内钩子）一并复核。
- 恢复一致性：新 session id 写回 `src/harness/runtime/checkpoint.py` 的 checkpoint，崩溃恢复指向新会话而不是旧会话。
- 降级路径：非 Docker 运行、host agent 或重启失败时，不退化为报错，而是把同一份简报文本用 steer 回注，
  并在 trace 里标出降级原因。
- 可观测：新增 trace 事件 `solver.context_restart`，字段含 `reason`、`from_session`、`to_session`、
  `brief_chars`、`degraded`。
- 验收：超过阈值的工具调用流触发一次重启；重启后 session id 变化并写回 checkpoint；旧会话文件仍在；
  host agent 路径记录降级而不是失败；简报不含凭据。
- 实现说明：容器由运行目录派生固定名字，重启前会先清理上一会话留下的容器（`src/harness/pi_container.py`）；
  被退役会话的原始记录归档为 `agent.stdout.restart-N.log` 与 `events.restart-N.jsonl`，其用量并入最终结果；
  本地 `harness run` 路径无法重启会话，改为把同一份简报 steer 回注并标记降级；工具调用触发条件在
  `src/harness/progress.py` 的 `context_restart_due()` 里逐次与 `tool_calls_at_last_restart` 比较。
  证据：`tests/test_executors.py`（重启、上限、用量合并）、`tests/test_pi_adapter.py`（容器清理）。

### A3 重复动作打断（ZCP-06；已被 RUN-09 取代）

> RUN-09 删除 `args_family` 记录、重复计数与 `solver.repetition_steer`。是否换招属于 Agent
> 的认知判断；Harness 只保留预算/墙钟止损。以下保留为历史设计记录。

- 目标：把"同一招磨到预算烧完"从观察变成干预。
- 设计：在 `src/harness/progress.py` 维护"按 family 的连续无进展计数"与"已提醒过的 family 集合"；
  连续 3 次同 family 且期间没有确认进展，就注入一次定向 steer（每个 family 每次运行只提醒一次）；
  同一 family 累计到 6 次时升级为 finalize 级 steer（要求收敛并写摘要），不硬杀进程。
- 状态落点：计数与冷却集合进 progress ledger（随 `progress.json` 落盘，崩溃后可恢复）。
- 进展重置：任何确认进展（接受提交、验证裁决、trusted progress）清零该 family 的计数。
- 可观测：新增 trace 事件 `solver.repetition_steer`，字段含 `family`、`consecutive`、`escalated`。
- 失败模式：不同 family 的正常轮换不能被误伤；命令族识别不到的命令（非 bash）不计数。
- 验收：合成同 family 连续事件只触发一次 steer；累计到阈值会升级；不同 family 的连续重试不触发；
  有确认进展后计数归零。
- 实现说明：阈值为 `REPETITION_STEER_STREAK = 3` 与 `REPETITION_ESCALATE_STREAK = 6`，计数随
  `progress.json` 落盘；证据见 `tests/test_solver_loop.py`。

### A4 知识库填充（ZCP-07）

- 目标：让 Agent 在容器里能按需 `ls` / `cat` 到真正的 payload 手册与方法论，而不是 21 行的占位说明。
- 技能手册：`skills/offensive/` 下每个技能扩写到 60-150 行，固定小节——适用条件、侦察与判定信号、
  payload 家族与变体、预期证据形态、操作边界与禁止事项、常见坑。
- 自研索引：`knowledge/README.md` 从一句话说明改成分类索引（web、network、cloud、pwn、crypto、forensics、misc）
  加自研方法论页；只写我们自己的正文，不复制第三方语料。
- 上游语料：新增一个取数脚本（计划路径 `scripts/fetch-knowledge.sh`），按 pinned commit 把
  PayloadsAllTheThings、HowToHunt 等拉到 `.harness/knowledge-cache/`（gitignored），运行时用现成的
  `--knowledge-root` 参数挂载。语料不进仓库、不进 CI 离线路径。
- 边界：知识目录仍是只读、无执行语义、不被 Harness 解析或排序；不得用"要读知识"换任何网络权限。
- 验收：每个 `skills/**/SKILL.md` 含全部必需小节；`knowledge/` 索引链接可解析；缺网时不依赖取数脚本；
  挂载后的内容对容器可见且不可写。
- 实现说明：7 个技能手册 57–62 行并含固定小节；`knowledge/` 拆成 methodology 与 categories 两层；
  上游语料由 `scripts/fetch-knowledge.sh` 按 pinned revision 拉取；证据见 `tests/test_knowledge_catalogue.py`。

## 3. B 段设计

### B1 失败路径看板（ZCP-08）

- 设计：在 `src/harness/world/context.py` 的非 focus 投影里新增有上限的 "## Ruled out" 段，渲染
  `refuted` 与 `superseded` 假设（上限 10 条，按更新时间取最近，截断时追加一行计数说明）；`Failures` 段保持原样。
- 兼容：没有 refuted/superseded 时输出与今天逐字节一致，避免破坏既有投影快照测试。
- 验收：被证伪的假设出现在 Ruled out；条数上限与截断计数生效；无相关记录时输出不变。
- 实现说明：上限 `max_ruled_out = 10`；证据见 `tests/test_world_context.py`。

### B2 战绩归因（ZCP-09）

- 设计：`result.json` 的 `executor_sessions[]` 每条补 `prompt`（system prompt 文件）与 `provider`；
  顶层新增 `executor_summary`，按 (prompt, provider, model) 聚合会话数、提交数、预算用量与 token；
  `harness report` 在该字段存在时打印这段汇总。
- 兼容：单会话运行不新增任何键，历史结果照旧可读。
- 验收：两个 executor 的 fixture 断言分组正确；单会话结果不含新键；缺失 provider 时字段为 null 而不是丢键。
- 实现说明：竞争运行额外写顶层 `executor_summary`，`harness report` 会先打印一段人类可读的归因；
  证据见 `tests/test_executors.py`。

### B3 收尾加固（ZCP-10）

- 设计：`solver.finalize` 提示词要求四段固定标题（已确认事实、未确认假设、已尝试并排除、未完成意图）；
  `result.json` 记录 `summary_present` 与缺失的段落列表；`harness tsec` 新增 `--seed-notes-from DIR`，
  把上一轮的 `agent.summary.md` 作为只读接力材料放进新运行的笔记目录。
- 边界：不再新增收尾会话，不改变"预算软限 + 宽限 + 补问"的既有语义。
- 可观测：摘要缺失时补一条 `solver.summary_incomplete` trace 事件。
- 验收：缺段落被记录并能从结果里读出；`--seed-notes-from` 指向的摘要出现在新运行的笔记目录且不可写；
  正常写摘要的一次运行不产生 incomplete 事件。
- 实现说明：结果里的字段是 `agent_report.present` 与 `agent_report.sections_missing`（顶层 `summary`
  已被世界摘要占用，故用 `agent_report` 命名）；接力材料写成 notes 目录下的 `prior-summary.md`；
  证据见 `tests/test_executors.py`。

## 4. C 段设计（已复核：默认关闭，逐条实现）

- 目标：把获奖方案真正吃分的那部分搬到我们这条链路上——工具输出落盘、压缩保留清单——它们是 Pi 的进程内钩子。
- 机制：把 Harness 自研的只读扩展放进运行期 agent 目录的 `extensions/` 子目录（该目录已挂到容器内
  `/run/harness/pi-agent`），不再默认传禁用扩展的开关。能力范围限定两件事：`tool_call` 时把超阈值的大输出
  落盘（默认 32000 字符触发、保留 600 字符预览），以及可选的 `session_compact` 保留清单注入。
- 信任论证（复核时逐条确认）：扩展是 Harness 作者代码、在 agent 容器内以 agent 权限运行；它不进入验证链，
  不能签发裁决，不新增网络能力，不接触凭据与 run root；容器仍拿不到权威工件。
- 开关：新增 `pi.harness_extension`（默认 false，显式开启），关闭时行为与今天完全一致，便于回退。
- 同步文档：更新 [security/execution-boundary.md](security/execution-boundary.md)，写清扩展的位置、能力上限与
  "不算可信证据"的定性；补容器边界测试。
- 不通过复核时的退路：只保留 A2 的会话级重启，接受"上下文只能靠简报回注、无法改写单条工具输出"的能力上限。

## 5. 接口与契约变更汇总

| 变更 | 类型 | 兼容性 |
| --- | --- | --- |
| `harness tsec` 去掉隐式 3600s 截断 | 行为修正 | 有意变更：声明的赛窗现在真的生效 |
| 新增 `--max-wall-time-seconds` | 新 CLI 参数 | 默认不设，向后兼容 |
| 新增 `--seed-notes-from DIR` | 新 CLI 参数 | 默认不设，向后兼容 |
| 新增 `pi.harness_extension` | Agent YAML 可选字段 | 默认 false，向后兼容 |
| `executor_sessions[]` 增加 `prompt` / `provider` | 结果契约扩展 | 仅竞争会话路径出现，单会话不变 |
| 新增 `executor_summary` | 结果契约扩展 | 仅竞争会话路径出现 |
| 新增 `summary_present` / `summary_sections_missing` | 结果契约扩展 | 附加字段 |
| 新增 trace 事件 `solver.context_restart` / `solver.repetition_steer` / `solver.summary_incomplete` | 事件扩展 | 只增不改，audit 与 trace 校验保持向前兼容 |

## 6. 工程量与天花板策略

- 现状：`tests/test_control_metrics.py` 钉住 `MAX_MODULES = 56` 与 `MAX_SRC_LINES`。
- 策略：保持模块数上限；新增行数在同一 PR 内以等量删除抵消，或者按实测值显式重测上限，并在 PR 里记录
  before/after 行数。不允许"顺手抬高上限"作为默认动作。
- 本轮实测：模块数保持 56（没有新增模块）；源码行数 14,770 → 15,508，已按实测值重测上限并在
  `tests/test_control_metrics.py` 的注释里记录差值来源。
- 不占额度：A4 的知识与技能是 markdown，不计入源码行数上限；但要注意仓库体积，上游语料只走 `.harness/` 缓存。

## 7. 验收与测试矩阵

| 项目 | 测试落点 | 关键断言 |
| --- | --- | --- |
| A1 | `tests/test_tsec_adapter.py` | 有效预算 = 声明预算；安全阀生效；未声明走默认 |
| A2 | `tests/test_solver_loop.py`、`tests/test_checkpoint.py` | 阈值触发一次重启；session id 写回；旧会话保留；非 Docker 降级 |
| A3 | `tests/test_solver_loop.py`、`tests/test_control_law_properties.py` | 同 family 只提醒一次；升级路径；不同 family 不触发 |
| A4 | 新增 markdown 结构校验 | 技能小节齐全；索引链接可解析；缺网不依赖取数脚本 |
| B1 | `tests/test_sqlite_world.py` 或世界投影测试 | refuted 出现在 Ruled out；上限生效；无记录时输出逐字节不变 |
| B2 | `tests/test_executors.py` | 汇总分组正确；单会话不新增键 |
| B3 | `tests/test_solver_loop.py`、`tests/test_tsec_adapter.py` | 缺段落可读；`--seed-notes-from` 生效且只读 |

回归命令：`pytest tests/test_docs_consistency.py tests/test_architecture.py tests/test_control_metrics.py -q`、
`ruff check .`、`pytest -q`；Docker 与 Pi 相关用例在本机自跳过，需在 CI 显式跑并记录。

## 8. 风险与未决

- 重启的隐性成本：每次重启都要重读简报，若 Threshold 定得太低，省下的退化可能抵不过损失的时间；阈值需要实测校准。
- 简报质量：接力简报由 Harness 从世界投影生成，若世界状态本身稀疏（Agent 没写 `world.observe`），简报会变薄。
  这也是 B1 与 A3 要一起做的原因。
- 上游语料的许可与体积：取数脚本只做 pinned 拉取，不复制正文；仍需在 PR 里记录许可证与来源。
- C 段已获复核并默认关闭；若运行时观测到执行边界问题，立即用开关回退到阶段一能力
  （会话级重启＋接力简报），并在赛后复盘如实记录。
- 本文只覆盖代码工程差距；赛制接入面（ZCP-01/02/04）仍以 [agent-engineering.md](agent-engineering.md) 为准。
