# 当前架构与黑客松获奖开源源码对比（代码级）

> 对标来源：[Yeti-791/Tsec-Hackathon](https://github.com/Yeti-791/Tsec-Hackathon) 官方资源仓导航出的
> **获奖队伍开源代码仓**（本文只读代码，不读答辩 PPT）。
> 目标赛场：[zc.tencent.com/hackathon](https://zc.tencent.com/hackathon)（王座协议·智能渗透闯关赛）。
> 本文只做赛前差距分析，唯一口径是"下一届能不能多拿分"；落地约束与排期见
> [agent-engineering.md](agent-engineering.md)，系统设计以 [agent-architecture-v2.md](agent-architecture-v2.md) 为准。
> 本文不改代码、契约与 roadmap。
> 两届 Top20 的架构与设计理念全景、以及理念层的 Harness 取舍见
> [zc-philosophy-comparison.md](zc-philosophy-comparison.md)。

## 0. 阅读约定与判定口径

- 只读各仓 **main 默认分支**，静态阅读，不运行对手代码；clone 落在 `/tmp`，不入本仓。
- 证据写作 `<仓>/<路径>`（第 N 行），行号对应 §1 表里 pin 住的 commit；上游提交后行号会漂移，需重新核验。
- 赛制接入面（官方 API、MCP、零界论坛赛道）不在本文范围：那是平台契约问题，不是代码工程问题。
- 判定用双口径，避免用工程优势掩盖失分点：
  - **对提分的影响**：这个维度上的能力差距会不会直接变成丢分。
  - **工程质量**：可维护性、可验证性、边界与测试。
- 判定是「本项目 vs 获奖仓集合」的相对结论，不是绝对评分；对手 README 自报的成绩本文不背书。

## 1. 对标对象与证据基线

读取日期 2026-10-15，全部为 `--depth 1` 浅克隆的 HEAD：

| 仓 | 战队 / 名次 | 语言与体量 | pinned commit | 可比性 |
| --- | --- | --- | --- | --- |
| m-sec-org/BreachWeave | 第二届 #1（一等奖，613 队） | TypeScript / Bun monorepo，947 文件 | `bc2d9502a2625c5ef354adc30243bd091341cc51` | Manager/Solver/Observer；另有 `pentest` 分支，本文只读 main |
| oritera/Cairn | 第二届 #3（唯一 AK，54/54） | Python，56 个 `.py` | `8e7e0ea67552383851dfcabfba0c4e9c8d007878` | 最小实现，黑板 Fact/Intent/Hint |
| chainreactors/aide-for-pentest | 第二届 #7（50/54） | 配置/skill，19 文件 | `958f7cfd02c709e1054d3775ae1a2a5b084f7bf8` | 只有 overlay，核心 runtime 未开源 |
| SanMuzZzZz/LuaN1aoAgent | 首届 #3（BinX）；自身 README 指向第二届 | TypeScript，约 3 万行 | `a035429766746866bcab30550cd0aac9c539be04` | 与本项目同为 Pi SDK 路线，最直接对标 |
| chainreactors/tinyctfer | 首届 #4（Antix） | Python，23 文件 | `11881fae68ef7740f362e9a7a58e5003cdb3df7e` | 100 行运行时 + Meta-Tooling |
| TJR181/Cruiser_public | 首届 #8（D@wnEdg3） | Python，2426 行 | `395006f8d424bbf5d95dff4233f6ad892f61661c` | 体量小，只作对照 |
| yhy0/CHYing-agent | 首届 #9 | Python，22k 行 + 3199 md | `0d2ee81fa783caf119cde1423e390552e32561c0` | 只比 agent/harness 工程，忽略论坛相关代码 |
| SickHackPark/SickHackShark | 首届 #10 | Python / LangGraph | `3f6541823d923e7479edf9107c43da7f456df60e` | 文件系统记忆层 + 上下文中间件 |
| Ed1s0nZ/CyberStrikeAI | 首届 #17（非前 10 参考组，6.6k 星） | Go，746 个 `.go` | `a4f01730dd7ef493eb2d8ea4e47b99ea92916c77` | toolguard/runlease/RAG/审计，工程面最可比 |
| m-sec-org/xbow-competition | 首届 #7 | 仅 1 个 README | `2c4d490d8ea5b2ea4c64474514eccae821baa1b7` | 无代码，按不可比处理 |

复现用的克隆命令（含 §5 的不可比条目）：

```bash
git clone --depth 1 https://github.com/m-sec-org/BreachWeave.git /tmp/tch-BreachWeave
git clone --depth 1 https://github.com/oritera/Cairn.git /tmp/tch-Cairn
git clone --depth 1 https://github.com/chainreactors/aide-for-pentest.git /tmp/tch-aide-for-pentest
git clone --depth 1 https://github.com/SanMuzZzZz/LuaN1aoAgent.git /tmp/tch-LuaN1aoAgent
git clone --depth 1 https://github.com/chainreactors/tinyctfer.git /tmp/tch-tinyctfer
git clone --depth 1 https://github.com/TJR181/Cruiser_public.git /tmp/tch-Cruiser_public
git clone --depth 1 https://github.com/yhy0/CHYing-agent.git /tmp/tch-CHYing-agent
git clone --depth 1 https://github.com/SickHackPark/SickHackShark.git /tmp/tch-SickHackShark
git clone --depth 1 https://github.com/Ed1s0nZ/CyberStrikeAI.git /tmp/tch-CyberStrikeAI
git clone --depth 1 https://github.com/m-sec-org/xbow-competition.git /tmp/tch-xbow-competition
```

## 2. 逐维度对比

### 2.1 上下文与记忆经济学

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| BreachWeave | `BreachWeave/packages/core/src/solver/extension/large-tool-result.ts`（第 5-6 行） | 单条工具结果超过 32000 字符就落盘成 spill 文件，上下文只留 600 字符预览 |
| BreachWeave | `BreachWeave/packages/core/src/solver/extension/pentest-compaction.ts`（第 16、25-43 行） | 接管 Pi 的 `session_compact` 钩子，并给出压缩保留清单：范围、资产、假设、已确认发现、凭据、证据路径、阶段、下一步；明确可丢弃冗长响应体与死路细节 |
| BreachWeave | `BreachWeave/packages/core/src/solver/extension/rtk-rewrite.ts`（第 25-49 行） | 在 `tool_call` 钩子里用外部 `rtk rewrite` 改写 bash 命令，在执行前就把体积压下来 |
| BreachWeave | `BreachWeave/packages/core/src/challenge/memory.ts`（第 4-5 行） | 记忆按 fact/evidence/failure/note/hint 分类，想法单独维护 pending/testing/verified/failed/skipped |
| LuaN1aoAgent | `LuaN1aoAgent/src/stores/artifact-store.ts`（第 42-70 行） | 输出按 sha256 去重落成工件，只把 preview 留在上下文；凭据类工件走单独的写入入口 |
| SickHackShark | `SickHackShark/src/agent/middleware/long_chain_wake_up.py`（第 8-57 行） | 最近 20 条工具消息里没有 `write_important_notes` 就主动干预，逼出一次笔记沉淀 |
| CHYing-agent | `CHYing-agent/chying_agent/brain_agent/retry_handoff_compiler.py`（第 1-30 行） | 重试同一题时把历史日志编译成约 3000 token 的续作报告：核心文件直接注入，大文件只给路径让 Agent 自己 grep |

本项目现状：`src/harness/extract.py`（第 17 行，每轮最多 25 条确定性事实）、
`src/harness/world/context.py`（第 14 行，离线 token 估算）、`src/harness/solver_loop.py`（第 571 行，每轮重写
`world.context.txt`）。有界、确定性、可测试，但**没有**会话级压缩、没有工具输出工件化、没有"重开会话 + 记忆回注"。
结构性约束：`src/harness/pi_adapter.py`（第 372 行）默认给 Pi 传 `--no-extensions`，因此对手那套
`tool_call` 与 `session_compact` 钩子在当前路径上不可用。

判定：对提分的影响 **明显落后**；工程质量 **领先**（我们已有的那部分是可测的确定性投影，而不是 prompt 文本）。

### 2.2 知识供给（KB / RAG / 技能目录）

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| BreachWeave | `BreachWeave/packages/core/src/config/skills/builtin/`（19 个技能包，226 个 markdown，约 60MB） | PayloadsAllTheThings 级别的 payload 语料随仓分发，按需读取 |
| CyberStrikeAI | `CyberStrikeAI/skills/`（23 个技能包）、`CyberStrikeAI/internal/knowledge/`、`CyberStrikeAI/knowledge_base/`（11 文件） | 技能包 + 完整向量 RAG 链路（indexer/embedder/rerank/retriever）；自带知识库只是种子，靠导入扩充 |
| CHYing-agent | `CHYing-agent/chying_agent/rag/compiled_kb.py`（第 1-20 行） | 明确不引入 embedding：markdown wiki + frontmatter 关键词匹配，"index.md 在中等规模够用" |
| aide-for-pentest | `aide-for-pentest/resources/tools/nuclei_templates/nuclei_templates.py`、`aide-for-pentest/resources/skills/ctf-ops/SKILL.md` | overlay 层给 nuclei 模板检索工具 + ctf-ops 技能 |
| Cairn | `Cairn/cairn/src/cairn/dispatcher/config.py`（第 40-56 行） | 反例：零 RAG、零 MCP，只把图状态注入 prompt |

本项目现状：`skills/offensive/` 7 个技能、`skills/examples/` 8 个技能，写的都是"何时适用/预期结论/操作边界"；
`knowledge/README.md` 是 `knowledge/` 目录里的唯一内容，没有 payload 语料。只读挂载点见
`src/harness/models.py`（第 152 行），提示词见 `prompts/zc-solver.md`。

判定：对提分的影响 **明显落后**（"不知道 payload 怎么写"是丢分主因）；工程质量 **持平**。

### 2.3 并行与调度

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| BreachWeave | `BreachWeave/packages/core/src/challenge/manager.ts`（第 57、1445-1465 行） | 单题最多 7 个 Solver 并行；planner 用 `planner_launch_solver` 工具带一份 solverHandoff 简报派活 |
| BreachWeave | `BreachWeave/packages/core/src/challenge/stats.ts`（第 14-40 行） | 按 solver/prompt/model 归因 token 用量与解题时长，形成"哪个 prompt 打得快"的战绩 |
| Cairn | `Cairn/cairn/src/cairn/dispatcher/config.py`（第 166-170、255 行）、`Cairn/cairn/src/cairn/server/services.py`（第 223-256 行） | 三层配额：全局 worker 上限、同时运行项目数上限、单项目 worker 上限；worker 租约带心跳，超时自动回收重分 |
| LuaN1aoAgent | `LuaN1aoAgent/src/controller.ts`（第 1258、1638 行）、`LuaN1aoAgent/src/planner-commands.ts` | 任务图 + 每任务 maxTurns；planner 追加预算要过累计分配上限校验 |
| tinyctfer | `tinyctfer/meta-tooling/service/python_executor_mcp.py` | 工具面收敛成"写 Python 代码执行"一条，避免多工具往返污染上下文 |

本项目现状：`src/harness/benchmark/tsec_adapter.py`（第 32、363 行，最多 3 题并行 + 信号量）、
`src/harness/runtime/executors.py`（第 137 行）与 `src/harness/models.py`（第 125 行，题目内最多 4 个会话，
预算均分、墙钟不切分）。

判定：对提分的影响 **持平**（题间 3 路 + 题内 4 路已在同一量级）；工程质量 **领先**。

### 2.4 收敛与止损

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| Cairn | `Cairn/cairn/src/cairn/dispatcher/config.py`（第 133-144 行） | 每类任务都有主超时 + conclude 兜底超时：主阶段超时后仍开一次收尾会话，把已确认事实写回图 |
| Cairn | `Cairn/dispatch.example.yaml`（第 33-36 行） | 单次 reason 最多产出 2 个新 intent（`max_intents`），防止一次规划炸出无限分支 |
| LuaN1aoAgent | `LuaN1aoAgent/src/epoch-budget-clock.ts`（第 21-56 行）、`LuaN1aoAgent/src/controller.ts`（第 889 行） | 预算时钟可暂停/恢复（等待时间不扣预算），另有全局运行时间上限 |
| BreachWeave | `BreachWeave/packages/core/src/solver/extension/scope-guard.ts`（第 47 行）、`BreachWeave/packages/core/src/solver/extension/challenge-observer/observer-loop.ts`（第 9-13 行） | 主 Agent 直接动作次数上限 6；observer 每 6 轮审查一次，提醒带冷却窗口，避免刷屏 |
| CHYing-agent | `CHYing-agent/chying_agent/claude_sdk/reflection.py`（第 44、740 行） | 重复动作分两级：L1 警告，L2 对高重复/低副作用的动作类直接硬阻断 |

本项目现状：`src/harness/solver_policy.py`（第 27、32、36 行：无进展 6 / 未确认进展 40 / 预算与低预算的纯止损律）、
`src/harness/budget.py`（第 23 行：超限后 180s 收尾宽限）、`src/harness/runtime/checkpoint.py`（第 278 行：对账）。
缺的正是对手最普遍的两件事：超时后的**收尾总结**与按步数或重复度的**分级干预**。

判定：对提分的影响 **落后**；工程质量 **领先**（止损律是纯函数，可做属性测试）。

### 2.5 证据与溯源

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| LuaN1aoAgent | `LuaN1aoAgent/src/stores/graph-store.ts`（第 1923-1943 行） | 图写入即校验：Vulnerability、成功的 Exploit、被证伪的 Hypothesis 必须带 evidenceRefs，否则整批 delta 拒绝落图 |
| CyberStrikeAI | `CyberStrikeAI/internal/audit/`（record/sanitize/retention/throttle）、`CyberStrikeAI/internal/attackchain/` | 审计记录带脱敏、保留策略与限流；攻击链可回放 |
| Cairn | `Cairn/cairn/src/cairn/server/models.py`（第 13-37 行） | Fact/Intent 带 creator/worker/心跳与 from→to 边，但没有验证等级，结论仍由提交结果判定 |

本项目现状：`src/harness/verification.py`（第 72、79-83 行：verified 裁决必须有证据，且 evidence 的
run/action/revision/producer 必须自洽）、`src/harness/runtime/evidence.py`（第 36-40 行：只有
task_verifier 与 benchmark_evaluator 能签发裁决）、`src/harness/trace.py`（第 11-30 行：trace 脱敏）。

判定：对提分的影响 **持平**（平台用 flag 判定，信任分层不直接加分）；工程质量 **领先**——对手是在图里加约束，
我们是有权威签发与证据引用的裁决，两者不在同一层。

### 2.6 隔离与出口边界

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| LuaN1aoAgent | `LuaN1aoAgent/src/connectivity/network-sandbox-manager.ts`（第 141、386 行）、`LuaN1aoAgent/src/scope.ts`（第 19-38 行）、`LuaN1aoAgent/src/connectivity/host-egress-broker.ts` | 每题独立网络沙箱；授权范围（CIDR/域名）显式解析、为空即报错；出口经宿主机 broker，另有 MITM 流量面 |
| CyberStrikeAI | `CyberStrikeAI/internal/toolguard/toolguard.go`（第 1-6、17-26 行）、`CyberStrikeAI/internal/runlease/scope.go`（第 1-9、21 行） | 工具执行前按规则正则拦截，包注释里明确它**不是**完整授权校验；把游离 worker 绑到 run 上并做取消确认 |
| tinyctfer | `tinyctfer/tinyctfer.py`（第 1-20、30-45 行） | 100 行运行时：只读挂配置、可写工作区，Agent 在容器边界内自由发挥 |

本项目现状：`src/harness/network_policy.py`（第 11 行：offline/target-only 等严格 profile，fail-closed）、
`src/harness/gateway.py`（第 208 行：工具面只有 file.read 与 file.write，且带策略上限）、
`src/harness/pi_container.py`（第 185-232 行：网络强制与 Gateway 兼容性检查），完整边界见
[security/execution-boundary.md](security/execution-boundary.md)。

判定：对提分的影响 **持平**（我们没有靶机流量采样；对手的 MITM 利于讲清利用链，但不直接加分）；
工程质量 **领先**。

### 2.7 可观测与复盘

| 仓 | 代码证据 | 做法 |
| --- | --- | --- |
| LuaN1aoAgent | `LuaN1aoAgent/src/web-server.ts`（3219 行）、`LuaN1aoAgent/src/web-trace-presentation.ts` | 实时 trace 工作台 + 因果图，推理、动作、工件逐条可翻 |
| CyberStrikeAI | `CyberStrikeAI/internal/monitor/`、`CyberStrikeAI/web/` | Web 看板 + 审计保留策略 |
| CHYing-agent | `CHYing-agent/chying_agent/observability.py`、`CHYing-agent/chying_agent/writeup_generator.py` | 观测初始化 + 自动 writeup + DB 记录 |

本项目现状：`src/harness/audit.py`（第 1 行：只读一致性审计，输出 consistent/contradictory/incomplete）、
`src/harness/trace.py`（append-only trace + 脱敏）、CLI 的 `harness report` 与 `harness audit-run`。
没有可视化界面，复盘靠读工件。

判定：对提分的影响 **落后**（不直接加分，但赛前调试与赛后复盘的产能是隐性分数）；工程质量 **持平**。

### 2.8 工程组织、测试与架构边界

| 仓 | 证据 | 做法 |
| --- | --- | --- |
| LuaN1aoAgent | 85 个测试文件 | 仓储/沙箱/控制器分层，测试面最宽 |
| CyberStrikeAI | 300 个 `*_test.go` | 覆盖最广，代价是 746 个 Go 文件的大仓 |
| BreachWeave | 29 个 `*.test.ts`，与实现同目录 | monorepo 分包，模块内测试 |
| Cairn | `Cairn/cairn/tests/`（12 个文件，含 mock 端到端与调度逻辑） | 最小实现，但把调度与协议都测了 |
| tinyctfer / Cruiser_public / CHYing-agent / aide-for-pentest | 0 个测试文件 | 竞赛型代码，靠现场跑通 |

本项目现状（§3 改进轮落地前的基线快照，当前值见 [roadmap/quality.md](roadmap/quality.md)）：27 个测试文件、
8890 行测试对应 14,770 行源码；`tests/test_architecture.py`（第 36、81、440 行：
依赖方向、被删子系统禁止回归、控制脊 import 闭包冻结）、`tests/test_control_metrics.py`（第 22 行：模块数上限 56）、
`ruff check .`。

判定：对提分的影响 **持平**（测试不直接得分，但决定改得快不快）；工程质量 **领先**。

## 3. 差距 → 提分优先级

赛制耦合项（ZCP-01 接入面核对、ZCP-02 限流与实例上限、ZCP-03 多题并行、ZCP-04 分级提示）不在本文范围，
状态以 [agent-engineering.md](agent-engineering.md) 为准，本文不改其状态与编号。

下表是按对手代码证据重排的**代码级差距**，沿用既有编号，新差距从 ZCP-08 起顺延：

| ID | 差距 | 对手证据 | 落点 | 状态 | 提分影响 |
| --- | --- | --- | --- | --- | --- |
| ZCP-05 | 会话级上下文压缩与记忆回注（含大输出工件化） | BreachWeave 的 large-tool-result 与 pentest-compaction、LuaN1aoAgent 的 artifact-store、CHYing-agent 的 retry-handoff 编译器 | 阶段一：阈值触发的新会话 + 接力简报（`src/harness/benchmark/runner.py`）；阶段二：需要 Harness 接管的 Pi 侧钩子（当前 `src/harness/pi_adapter.py` 第 372 行默认禁用扩展） | 阶段一已落地（`tests/test_executors.py`）；阶段二待 maintainer 复核 | 高 |
| ZCP-07 | 知识供给：payload 手册与方法论只读目录 | BreachWeave 的 builtin 语料（226 md）、CyberStrikeAI 的 23 个技能包、CHYing-agent 的关键词 wiki | `skills/offensive/` 手册化 + `knowledge/` 方法论与分类索引；上游语料按 pinned revision 取到缓存后只读挂载 | 已落地（`tests/test_knowledge_catalogue.py`） | 高 |
| ZCP-06 | 重复动作打断（不止 critique） | CHYing-agent 的 L1 警告与 L2 硬阻断、SickHackShark 的 long-chain wake-up | `src/harness/solver_loop.py` 的 steer 通道 + 已有 `args_family` 的连续计数 | 已落地（`tests/test_solver_loop.py`） | 高 |
| ZCP-08 | 失败/已排除路径的可读看板 | BreachWeave 的 failure 类目与 idea status、CHYing-agent 的死路提取 | `src/harness/world/context.py` 的有界 Ruled out 段 | 已落地（`tests/test_world_context.py`） | 中 |
| ZCP-09 | 会话级用量归因与 prompt/model 战绩 | BreachWeave 的按 prompt/model 用量与解题时长统计 | `result.json` 的 `executor_sessions[]` 补 prompt/provider，并新增按 (prompt, provider, model) 聚合的 `executor_summary` | 已落地（`tests/test_executors.py`） | 中 |
| ZCP-10 | 超时后的收尾总结兜底 | Cairn 的 conclude 超时、LuaN1aoAgent 的可暂停预算时钟 | 加固已有链路：四段固定标题、`agent_report` 记录缺失段落、`--seed-notes-from` 复用上一轮摘要 | 已落地（`tests/test_executors.py`） | 中 |
| ZCP-11 | 墙钟被硬编码腰斩（有效预算 ≠ 声明预算） | Cairn 的任务超时配置、LuaN1aoAgent 的可暂停预算时钟 | 删除 `min(wall_time, 3600)`；`--max-wall-time-seconds` 作为运维安全阀 | 已落地（`tests/test_tsec_adapter.py`） | 高 |

排序口径：ZCP-05 与 ZCP-07 决定"能不能打出来"，ZCP-06 决定"打偏后能不能拉回"，ZCP-08/09/10 是稳健与赛后项。
ZCP-05 若要打开 Pi 扩展，属于执行边界变更，按 [security/execution-boundary.md](security/execution-boundary.md)
走 maintainer 复核。

## 4. 本项目已领先但不直接加分

- **信任分层与证据引用**：裁决必须有独立证据且字段自洽（`src/harness/verification.py`）。对手里只有
  LuaN1aoAgent 做到了同层级的写入校验，其余都是"模型说什么就是什么 + 平台兜底"。
- **恢复与幂等**：checkpoint 与未决动作对账（`src/harness/runtime/checkpoint.py`）。对手里只有 Cairn 用租约心跳
  做了部分等价物，没有一家做幂等重放对账。
- **架构边界可执行**：依赖方向、被删子系统、控制脊 import 闭包都由 `tests/test_architecture.py` 强制。
- **出口与凭据边界**：严格网络 profile + 工具面收敛 + 凭据只在 Harness 侧（`src/harness/network_policy.py`、
  `src/harness/gateway.py`）。

一句话：这些是"比赛之外"的资产，值钱但不变成分数。不要把它们的工程自信读成赛道优势——§2.1/2.2/2.4 的差距
才是丢分的地方。

## 5. 不可比与未覆盖

- 第二届 #2 Sniper（天翼安全）、#4 ToBeNumberOne（京东）、#5 奇盾明焰、#6 运行完成、#8 清华、#9 星空有云、
  #10 戴夫的后花园：官方资源仓只有答辩 PPT，没有对应开源代码仓，无法做代码级比对。
- 首届 #2 xjtuHunter：资源仓给的是 GitHub 组织链接，组织下没有公开仓。首届 #6 Neuro-Sploit：组织下是平台与
  评测类仓库（API server、scoreboard、xbow-validation-benchmarks），不是参赛 Agent 实现。
- 首届 #7 m-sec-org/xbow-competition：仓库只有一个 README，无代码，按不可比处理（该团队另有可读仓：
  首届 tinyctfer 与第二届 BreachWeave）。
- aide-for-pentest：只开源 overlay（任务、技能、模板、worker 骨架），核心 runtime 未开源，只能比适配层。
- BreachWeave 的 `pentest` 分支本文未读；LuaN1aoAgent 的名次表述在官方表（首届 #3 BinX）与项目自述
  （指向第二届赛场）之间不一致，本文按"首届 #3 参赛队，v2 为后续重写"记录。

## 6. 未证实项与风险

- 静态阅读只能证明"代码里有什么"，不能证明"场上有效"；本文不下"谁更强"的结论，只下"哪些能力我们没有"。
- 未运行对手代码，其 README 里的解题率、成本、排名均未复现，只作上下文。
- 语言与体量不同（Go / TS / Python），文件数与行数只作规模参考，不作质量结论。
- 只覆盖 §2 的 7+1 个维度；Web UI 细节、插件市场、平台接入面不在其中。
- 全部结论以 2026-10-15 的 commit 为准，对手仓继续演进后需重新核验行号与能力清单。

## 7. 参考

- [agent-architecture-v2.md](agent-architecture-v2.md)（当前设计：Agent-Native Harness）
- [agent-engineering.md](agent-engineering.md)（赛场落地、ZCP 梯子与排期）
- [tsec.md](tsec.md)（TSec 集成与消融矩阵）
