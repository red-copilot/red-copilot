# 两届 TCH 获奖项目架构与设计理念：全景扫描与 Harness 对标

> 对标来源：[Yeti-791/Tsec-Hackathon](https://github.com/Yeti-791/Tsec-Hackathon)——官方资源仓收录的两届决赛答辩 PPT、开源仓导航与公开复盘文章。
> 互补文档：[zc-architecture-comparison.md](zc-architecture-comparison.md)（代码级能力差距与 ZCP 项）、[zc-improvement-design.md](zc-improvement-design.md)（差距的实现设计）。本文补三件事：只存在于 PPT 里的理念材料、无代码队伍的覆盖、两届之间的演进脉络，并给出理念层的 Harness 取舍。
> 边界：只读静态材料，不运行对手代码、不整段复制材料；不改代码、契约与 roadmap 状态；名次、成本、解题数按官方材料或队伍自述照录，未复现。

## 1. 阅读约定与语料索引

**证据写法**：仓内代码/文档写 `<仓>/<路径>`（第 N 行）；答辩 PPT 写 `S<届>·<战队>`（slide N）；公开复盘文章标注为二手材料。图像型 PPT 先经本机 OCR 转写再引用，个别字词可能存在识别偏差。

**覆盖口径**：官方导航列出且有 PPT、开源仓或答辩材料之一的队伍全部收录；两届 Top20 内无任何公开材料的名次在第 7 节列出。阅读深度三级：A＝读代码，B＝读 README/文档/目录，C＝仅答辩 PPT。

**pin 口径**：全部仓于 2026-10-16 记录 `--depth 1` HEAD。官方资源仓本身 pin 在 `3d380f32ca21`。

### 1.1 首届（2025-11）收录条目

| 名次 | 战队 | 材料 | 开源仓（pin）与定位 |
| --- | --- | --- | --- |
| 1 | 长亭外（长亭科技） | C＋二手报道 | 无公开仓。“探索-规划-利用”三阶段与多智能体并行；主理念 “Programming, not Prompting” |
| 2 | xjtuHunter | C | 组织下无公开仓。场景感知黑盒渗透：页面探索→场景塑造→漏洞检测 |
| 3 | BinX（广州大学） | A | [LuaN1aoAgent](https://github.com/SanMuzZzZz/LuaN1aoAgent) @ `a03542976674`。Planner-Executor-Reflector＋任务图/因果图；v2 重写为 P-E-O |
| 4 | Antix（chainreactors） | A | [tinyctfer](https://github.com/chainreactors/tinyctfer) @ `11881fae68ef`。100 行 runtime＋Meta-Tooling；“Intent is All You Need” |
| 5 | Pachinko | C | 无公开仓。ReAct MVP＋Skill/动态知识库；“Modularization vs End2End”（官方导航未列名次） |
| 6 | NeuroSploit | C＋评测侧仓 | 无参赛 Agent 实现仓。Lead＋专家子 Agent＋PentestReMem＋Burr 状态机＋自动评估系统 |
| 7 | ai小分队（绿盟） | C＋B | [xbow-competition](https://github.com/m-sec-org/xbow-competition) @ `2c4d490d8ea5`（仅 README）。“蜂群思维”：共享笔记本＋防沉迷/防路径依赖 |
| 8 | D@wnEdg3 | A | [Cruiser_public](https://github.com/TJR181/Cruiser_public) @ `395006f8d424`。ReAct＋5 Agent 竞赛＋步数阈值多级队列＋分级提示 |
| 9 | yhy | A | [CHYing-agent](https://github.com/yhy0/CHYing-agent) @ `0d2ee81fa783`。人机协作＋主攻/顾问双 Agent＋Kali 真工具 |
| 10 | sickhack | A | [SickHackShark](https://github.com/SickHackPark/SickHackShark) @ `3f6541823d92`。文件系统记忆＋30k/100k 上下文清洗＋渐进式输入 |
| 15 | 华科金银湖 | B | [newmapta](https://github.com/HUST-JYHLab/newmapta) @ `011eef16d793`。CrewAI ReAct＋方法论引导多 Agent＋ARG 知识库 |
| 16 | 瀚海星云 | B | [sub-agent-autopt](https://github.com/yyy1mu/sub-agent-autopt) @ `5cf59857e537`。Coordinator/Planner/Executor＋Docker 沙箱工具 |
| 17 | 小白战队 | A | [CyberStrikeAI](https://github.com/Ed1s0nZ/CyberStrikeAI) @ `63f3797ea85d`。Go 平台化：Eino 编排＋100+ 工具＋HITL/审计/拦截/租约 |
| 18 | HRP（Nepnep） | B | [H-Pentest](https://github.com/hexian2001/H-Pentest) @ `1d775a2c787c`。Meta/Strategic Supervisor＋Worker；52+ 知识库与 RAG |

说明：首届导航表未列 1、5、11-14、19-20 名。第 1 名“长亭外”由长亭科技公开报道确认；Pachinko 的名次无公开来源，不推断。第 6 名公开的是平台与评测侧仓库，不是参赛 Agent 实现。

### 1.2 第二届（2026-04）收录条目

| 名次 | 战队 | 材料 | 开源仓（pin）与定位 |
| --- | --- | --- | --- |
| 1 | ai小分队（绿盟） | A | [BreachWeave](https://github.com/m-sec-org/BreachWeave) @ `bc2d9502a262`。Manager/Solver/Observer＋双层状态板＋三层上下文管线＋Ralph-Loop |
| 2 | Sniper（天翼安全） | C | 无公开仓。Self-Evolving Kill Chain：DeepAgent 主从＋8 层中间件＋四重知识体系＋自研工具 |
| 3 | Bytex | A | [Cairn](https://github.com/oritera/Cairn) @ `8e7e0ea67552`。黑板 Fact/Intent/Hint、无角色无流程；全场唯一 AK |
| 4 | ToBeNumberOne（京东） | C | 无公开仓。“Less Structure More Intelligence / Less Tech More Business”；平台化 C2 与统一工具网关（据复盘文） |
| 5 | 奇盾明焰 | C | 无公开仓。控制优先 Harness：主线 Agent＋Advisor/Alignment/KB Analyst 旁路＋笔记压缩 |
| 6 | 运行完成（中国电信） | C | 无公开仓。领域知识＋路径记忆＋多路径协同；全局黑板与分支黑板；断点续跑/模型无关/资源可控 |
| 7 | For future（ChainReactors） | A（overlay） | [aide-for-pentest](https://github.com/chainreactors/aide-for-pentest) @ `958f7cfd02c7`。“Less Than Nothing”：FSM＋自然语言边、overlay 注入、外层调度＋内层 worker |
| 8 | ╮(￣▽￣)╭（清华） | C | 无公开仓。攻击路径规划＋知识库利用；Planner 维护攻击路径树并按反馈更新 |
| 9 | 星空有云 | C | 无公开仓。Dispatcher＋多 Lead（Web/CVE/Network/AD）＋recon/attack 子 Agent＋Monitor |
| 10 | 戴夫的后花园 | C | 无公开仓。“代码约束>提示词约束”：事件驱动管控链＋四维循环检测＋独立顾问纠偏；工具执行被视为 Agent 的“骨骼” |
| 17 | 爱吃大红袍茶叶蛋 | B | [LingXi](https://github.com/adrian803/LingXi) @ `6b81f3cfcd8d`。拉题→侦察→执行→纠偏→提交→沉淀闭环；主赛场与零界双赛道 |
| 18 | 青松 | B | [llmnor](https://github.com/QingHeZhiZhou/llmnor) @ `acfe4bc6986b`。“LLM 微内核运行时”：模型决策、宿主定能力边界、工具优先、可追溯 |
| 19 | 云南大学/西南石油大学 | B | [cloudever 组织仓](https://github.com/CloudEver-Team/cloudever_tecent_penetration_2026_4) @ `54cccc7dd3e0`；[TPT-Agent](https://github.com/CloudEver-Team/TPT-Agent) @ `4f7980152692`；[RedCortex](https://github.com/CloudEver-Team/RedCortex) @ `238a7c051694`。双实现：任务驱动编排＋固定流程图上的自主选路与文件化状态 |
| 20 | 别用假装努力掩盖懒惰 | B | [hackathon-pentest](https://github.com/Threonine/hackathon-pentest) @ `953b8d075861`。Orchestrator＋Web/内网专家子 Agent；与平台 API 深度耦合 |

说明：第二届导航表只列出 1、3、7、17-20 名的开源仓；2、4、5、6、8、9、10 名只有答辩 PPT。表内名次为线上赛名次；Cairn 复盘文另给出全国总成绩第三（含线下决赛）。

### 1.3 延伸与范围外

- 官方导航另有首届 25 名 [BUUCTF_Agent](https://github.com/MuWinds/BUUCTF_Agent)、35 名 [AgentNote](https://github.com/C1JC/AgentNote)，已超 Top20 范围，只登记链接不做分析。
- 官方仓内的零界论坛工具包、平台 API/MCP 文档属于赛道接入面，不是获奖方案架构；其状态以 [agent-engineering.md](agent-engineering.md) 的 ZCP-01/02/04 为准。

## 2. 两届赛制与代际背景

两届比赛换了命题，读同一批架构时会看到完全不同的侧重点：

- **首届（2025-11）**：238 支战队、518 名选手；赛题来自 XBOW benchmark，每天随机换题、限制国内模型、每日两个时段（每段 3 小时、6-8 题、无外网）——见 Cairn 复盘文与 `Cruiser_public` 答辩 PPT（slide 5）。命题是“单题闭环能不能稳定跑通”：状态主要活在上下文与笔记里，行动是简单工具调用，控制靠 ReAct、重试和少量规则（复盘文《两届TCH之后》）。
- **第二届（2026-04）**：610 支战队、1345 名选手（Cairn README 第 120-121 行；BreachWeave README 记 613 支，口径差异见第 7 节）。54 个 flag、四赛区闯关、排名衰减计分：首解（一血）+20%、31 名后 -80%、查看提示 -10%（CHYing-agent README“两届赛制对比”一节）；不限模型，成本第一次成为一等变量；另有“零界”平行赛场。命题升级为“长链路、多阶段、强不确定环境下的持续推进与收敛”（复盘文）。
- **平台硬约束**（第二届平台文档）：同时在跑实例 ≤3、全接口 3 req/s、提示扣分、多 flag 按 flag 计分；这些约束在 [agent-engineering.md](agent-engineering.md) 第 4 节已整理，本文不重复。

判断：首届拼“能不能解出来”，第二届拼“解得够不够快、稳不稳、贵不贵”。因此第二届 Top 方案的共性不是某个拓扑，而是把状态、行动、控制三层都工程化；分歧集中在工程化到什么程度（第 3、4 节展开）。

## 3. 架构谱系

把 28 个条目放回同一条工作循环（状态→决策→行动→反馈→新状态）里看，反复出现的解法只有五类：**状态外置、上下文降载、控制硬约束、结束判定外置、并发即探索**。它们与具体拓扑无关，因此先按维度展开，每条结论至少给两个独立队伍的出处。

### 3.1 编排拓扑：五族并存，名次不挑拓扑

| 族 | 代表队伍 | 主要证据 | 结构要点 |
| --- | --- | --- | --- |
| 角色编排型 | BreachWeave、LuaN1aoAgent、Sniper、H-Pentest、hackathon-pentest | `BreachWeave/README.md`（第 38-77 行）；`LuaN1aoAgent/README.md`（第 44-46 行）；S2·Sniper（slide 10-11）；`H-Pentest/README.md`（第 27-42 行） | Manager/Planner 管全局，Solver/Executor 并行执行，Observer/Supervisor 旁路监督 |
| 黑板/涌现型 | Cairn、S1 ai小分队、aide（ant_colony pattern） | `Cairn/README.md`（第 20、55-63 行）；`Cairn/docs/specs/server-protocol.md`（第 17-21 行）；S1·ai小分队（slide 6-7） | 没有中央调度；共享状态就是协作协议，个体用信息素式写入间接同步 |
| FSM/程序型 | aide/For future、长亭外、京东 | S2·For future（slide 9、11、12、14）；`aide-for-pentest/resources/patterns/ralphloop.yaml`（第 1 行起）；S1·长亭外（slide 5-10） | 宏观行为由模式/模板约束；节点与边用自然语言表达，领域知识走 overlay 注入 |
| 单 Agent＋元工具/微内核型 | tinyctfer、llmnor、Pachinko、CHYing-agent | `tinyctfer/tinyctfer.py`（第 1-19、81-99 行）；`llmnor/README.md`（第 15-21 行）；S1·Pachinko（slide 8-15）；`CHYing-agent/README.md`（第 56-95 行） | 工具面收敛到一个执行入口或一层可见性控制，能力来自沙箱与工具集而非角色数量 |
| 调度器/队列型 | D@wnEdg3、Cruiser、星空有云、For future（外层）、LingXi | `Cruiser_public/README.md`（第 10-11 行）；S1·D@WNEDG3（slide 9）；S2·星空有云（slide 7、10-12）；S2·For future（slide 5） | 外层系统管题、实例与优先级，内层 Agent 只负责单题解题 |

几个值得单独记下的点：

- **第二届前三名恰好是三种不同拓扑**：BreachWeave 是显式角色编排，Sniper 是主从 Agent＋8 层中间件，Cairn 是无角色黑板。拓扑与名次之间没有单调关系；三家的共同点是状态显式化、上下文治理、结束判定外置。
- **"薄控制＋厚状态"的另一种表述**：星空有云把顶层设计写成 Dispatcher 只做调度不做题、Lead 指挥、Operators 执行、Monitor 只读；状态走 File-as-Bus 共享工作区，按角色严格分区写入（S2·星空有云 slide 7-8）。
- **角色分工的用途在变**：BreachWeave 的 Solver 是并行探索单元（`packages/core/src/challenge/manager.ts` 第 57 行，默认最多 7 个 Solver、最多 3 道活跃题）；Cairn 明确把 Worker 当并发模型而不是分工模型（复盘文《两届TCH之后》：多 Agent 的价值是并行探索与独立上下文，不是人类岗位模拟）；CHYing 的 MCP 可见性控制是为了隔离工具描述、保住 Orchestrator 的注意力，不是按技能切人（`CHYing-agent/README.md` 第 78-95 行）。
- **Cairn 的协议把"任务类型"收敛成三个动作**：`bootstrap`（初始直接尝试）、`reason`（读图判断完成/提出新 Intent）、`explore`（认领一个 Intent 产出 Fact）——见 `Cairn/README.md` 第 76-78 行与答辩 PPT（slide 13-14）。Agent 每次只看到"当前图＋三种任务指令之一"，没有任何一行代码告诉它渗透测试的流程（S2·Bytex slide 14）。

### 3.2 记忆与上下文经济学

**双层状态（事实与假设分离）** 是被重复最多的设计：

- BreachWeave 把状态拆成 Idea 板（pending/testing/verified/failed/skipped）与 Memory 板（fact/evidence/failure/note/hint）——`packages/core/src/challenge/memory.ts`（第 4-5 行），答辩 PPT slide 12 给出两层看板的完整视图。
- Cairn 用 Fact/Intent/Hint 三原语表达"已确认的事实、待探索的方向、外部判断"，且 Fact 只增不改，状态变化通过追加新 Fact 表达（`Cairn/README.md` 第 55-57 行；`Cairn/docs/specs/server-protocol.md` 的 Hint 与状态变化小节）。
- 运行完成的 Path Map＋Global Memory、京东的 memory.md、天翼的 Memory 中枢（复盘文对第二届状态层的归纳）是同一思路的不同实现。

**上下文降载** 的主流做法是三层：

1. **改写**：BreachWeave 在 `tool_call` 钩子里用外部 `rtk rewrite` 把 bash 命令输出改写成 AI 友好形式（`packages/core/src/solver/extension/rtk-rewrite.ts` 第 25-49 行；S2·BreachWeave slide 14）。
2. **落盘**：单条工具结果超过 32k 字符就写成 spill 文件，上下文只留 600 字符预览（`packages/core/src/solver/extension/large-tool-result.ts` 第 5-6 行）；LuaN1ao 的大输出按 sha256 内容寻址成工件，按任务去重、预览进上下文、凭据走单独写入入口（`LuaN1aoAgent/src/stores/artifact-store.ts` 第 49-64 行；README 第 164 行）；Sniper 的中间件把过大工具输出自动存文件并生成引用链接（S2·Sniper slide 11）；Cairn 协议直接规定 Fact 文本应引用原始文件的路径（`Cairn/docs/specs/server-protocol.md` 的 Fact 小节）。
3. **摘要/压缩**：BreachWeave 接管 `session_compact` 钩子并规定"必须保留什么、可以丢弃什么"（`packages/core/src/solver/extension/pentest-compaction.ts` 第 16-43 行）；S1 ai小分队在"长轮次认知退化"时记录关键线索、重置上下文、回注恢复（S1·ai小分队 slide 9）；奇盾明焰专门设了一个笔记 Agent 做压缩（S2·奇盾明焰 slide 11）。

red-harness 的对应物是 ZCP-05 阶段一：按工具调用数与预算比例触发新会话，重写"已确立事实/已排除路径/开放意图/指针"的接力简报（落点与验收见 [agent-engineering.md](agent-engineering.md) 第 5 节）。差别在于对手把钩子放进了 Agent 运行时内部，我们目前只能做到会话级重启。

**失败边界** 也被当成一等状态：BreachWeave 有 `failure` 记忆类型和 `failed` Idea 状态；Cairn 允许把"SSH 和 SQL 注入方向均已排除"写成 Hint 供后续 Worker 降低读图成本（`server-protocol.md` 的 Hint 小节）；红队靶场自己的失败看板（ZCP-08）已在 `src/harness/world/context.py` 落地。

### 3.3 控制、收敛与收尾

**重复与停滞干预** 的分级阈值在头部方案里高度一致：

| 队伍 | 机制 | 出处 |
| --- | --- | --- |
| BreachWeave | 主 Agent 直接动作上限 6 次；Observer 每 6 轮审查一次、提醒冷却 6 轮、12 轮内不重复同类提醒；Ralph-Loop 最多续跑 10 次 | `scope-guard.ts`（第 47 行）；`observer-loop.ts`（第 9-13 行）；`ralph-loop.ts`（第 7-10 行） |
| CHYing-agent | L1 阈值 3 次软提醒、L2 阈值 5 次硬阻断（只对 path_scan/config_read/s3_enum）；每 15 次调用做一次 checkpoint | `chying_agent/claude_sdk/reflection.py`（第 45-48 行） |
| SickHackShark | 最近 20 条工具消息里没有"重要笔记"写入就注入行为校准消息 | `SickHackShark/src/agent/middleware/long_chain_wake_up.py`（第 13、42-46 行） |
| S1 ai小分队 | 同一命令连续调用超限后 hook 工具结果、注入干预；同题被解出 N 次后强制清空该题笔记 | S1·ai小分队 slide 9-10 |
| Cairn | 单次 `reason` 最多产出 2 个新 Intent，防止一次规划炸出无限分支 | `dispatch.example.yaml`（第 36 行） |

**结束判定外置** 是共同底线：BreachWeave 的 Ralph-Loop 把"任务是否结束"从模型主观判断中抽离，只有系统基于目标达成、证据充分性、状态一致性与终止权限四项检查后才允许结束（S2·BreachWeave slide 13）；Cairn 以 goal 被事实满足作为 `complete` 条件（`Cairn/README.md` 第 76-78 行）；red-harness 对应 `src/harness/solver_policy.py` 的纯止损律（[agent-architecture-v2.md](agent-architecture-v2.md) 第 4 节）。

**超时收尾** 是第二届才补齐的一课：Cairn 的 `bootstrap` 与 `explore` 都是双阶段——主阶段超时或解析失败后，用同一 session 进入 `conclude` 兜底，把已确认的发现落成 Fact，预算 90 秒（`Cairn/docs/specs/dispatcher-design.md` 第 21 行；`Cairn/dispatch.example.yaml` 第 28-31 行；答辩实录见 S2·Bytex slide 19）；red-harness 对应 ZCP-10 的结构化收尾摘要与缺失段落可观测（[agent-engineering.md](agent-engineering.md) 第 5 节）。

**预算时钟**：LuaN1ao v2 的 epoch budget clock 支持暂停/恢复，等待时间不扣预算（`LuaN1aoAgent/src/epoch-budget-clock.ts` 第 49-64 行）；运行完成把"断点续跑、模型无关、资源可控"写进方案（S2·运行完成 slide 12）；red-harness 对应 `src/harness/budget.py` 的超限收尾宽限。

### 3.4 知识供给：注入与留白两派

**注入派** 在头部队伍中占多数：

- BreachWeave 随仓分发 226 个 markdown、约 60MB 的技能语料（`packages/core/src/config/skills/builtin/`），包含 AD 渗透、内网渗透、nuclei、JWT、payload 研究等技能包。
- Sniper 把知识拆成 skill 系统、经验自蒸馏、向量 RAG、Memory 四层，并做了 15,000+ 指纹到 5,000+ 高危漏洞插件的"识别即检测"映射（S2·Sniper slide 12-13）。
- NeuroSploit 内置 40+ OWASP CheatSheets 与 50+ 利用脚本库（S1·NeuroSploit slide 7）；SickHackShark 引入 PortSwigger 教材、PayloadsAllTheThings、HowToHunt（S1·sickhack slide 7）；aide 提供 nuclei 模板检索工具与 `ctf-ops` 技能（`aide-for-pentest/resources/`）；CyberStrikeAI 有完整向量 RAG 链路（README 第 123 行）；H-Pentest 带 52+ 知识库（README 第 19、93 行）。
- 天翼的复盘——把 `weaver@2023` 写成字面值导致 Agent 只会照抄 `weaver@2024`——说明知识工程的关键不是"有没有库"，而是**表示方式**：模式（"weaver@年份"）才可以枚举，字面值不能（S2·Sniper slide 21）。

**留白/轻检索派** 只有少数，但成绩是最好的之一：

- Cairn 明确"zero MCP tools, zero RAG, zero predefined agent roles"，仍然拿到唯一 AK（`Cairn/README.md` 第 124 行）。
- CHYing 只用 markdown wiki＋frontmatter 关键词匹配，不引入 embedding（`chying_agent/rag/compiled_kb.py` 第 1-6 行，注释里直接引用 Karpathy"index.md at moderate scale"的判断）。
- S1 ai小分队把"简单架构＋强效控制＝好的工程实践"写进结论页（S1·ai小分队 slide 13）；For future 把"零领域知识"当作主动排除：知识库不塞进核心，走 overlay 注入（S2·For future slide 10-11）。

判读：知识供给是速度与覆盖的杠杆，不是正确性前提；留白派的前提是"通用知识已在模型里"（For future 复盘文与 PPT slide 8），注入派的前提是"长尾 payload 与产品知识模型不会现查"。两边都有高分样本，因此它属于 Agent 侧的内容决策，而不是 Harness 的机制。

### 3.5 隔离、工具与动作可靠性

- **沙箱**：tinyctfer 用 Ubuntu 桌面＋安全工具＋Python Executor MCP＋VNC 的沙箱，Claude 配置只读挂载、工作区读写（`tinyctfer.py` 第 34-46 行）；LuaN1ao v2 默认 per-Task Docker Executor、私有内网、Gateway 是唯一出口，显式 `docker` 模式在缺镜像时 fail-closed（README"Sandbox Isolation"节）；Cairn 每个项目一个容器、也支持 local 模式跑在本机 CLI 上（README"Local mode"节）；BreachWeave 把控制面与 Kali 执行面解耦，每个 Solver 独立 workspace（S2·BreachWeave slide 6）。
- **工具面**：tinyctfer 对 Agent 只暴露"写 Python 代码执行"一个入口，浏览器/终端/代理/笔记封装成 Python 库（S1·Antix slide 11-12）；CHYing 用 `visibility: subagent:browser` 把 30 个 Chrome MCP 工具全部藏起来，让 Orchestrator 只看到一个 browser 工具（`CHYing-agent/README.md` 第 78-95 行）；S1 yhy 明确反对"每个工具都封装成 MCP"，理由是工具描述会吃掉上下文（S1·yhy slide 8）；Cairn 的容器里装满工具，但协议层只有黑板三对象（复盘文）。**共同点：工具面服务于注意力经济，不是越多越好。**
- **动作可靠性**：Cairn 让 Dispatcher 做唯一协议写入者，Agent 不认领、不心跳、不直接调 API，协议层负责认领、心跳、超时回收（`Cairn/docs/specs/dispatcher-design.md` 第 13、20 行）；CyberStrikeAI 用 runlease 把异步工具 worker 绑到任务运行上、把"取消未确认"显式建模为错误（`CyberStrikeAI/internal/runlease/scope.go` 第 1-9、14-15 行），并用 toolguard 在执行前做规则拦截，且明确声明它不是完整的授权校验（`CyberStrikeAI/internal/toolguard/toolguard.go` 第 1-6 行）；Sniper 为后渗透自研了 C2、SOCKS5 隧道与 WebShell 管理器，并给出执行/目标/认知/行为四维边界（S2·Sniper slide 13-14）。

### 3.6 验证、证据与复盘

- **写入即校验**：LuaN1ao v2 规定已确认的 Vulnerability 与成功的 Exploit 必须有 `evidenceRefs`，否则整批图变更拒绝落库（`LuaN1aoAgent/README.md` 第 124-126 行；`LuaN1aoAgent/src/stores/graph-store.ts` 第 435-455 行的引用合并与校验）；BreachWeave 的 MemoryEntry 带 `refs` 字段（`memory.ts` 第 14 行），提交动作走 host bridge 而不是直接信任模型文本（`ralph-loop.ts` 第 49-62 行的完成检查）。
- **审计与保留**：CyberStrikeAI 的 `internal/audit/` 提供记录、脱敏、保留、限流四个子模块（README 第 131-132 行），攻击链可回放；TPT-Agent 把任务过程、执行事件、关键输出持久化到 SQLite（TPT README 第 23 行）；Sniper 用 Langfuse 抓 toolcall 链路与 token/成本（S2·Sniper slide 11）。
- **复盘产能**：LuaN1ao 的实时 trace＋因果图（README 第 73 行）、CyberStrike 的 Web 看板、BreachWeave 的模型/Prompt 战绩面板（`stats.ts` 第 20-24、82-83 行的按模型/提示词聚合；S2·BreachWeave slide 7）、奇盾的纠偏案例回放（S2·奇盾明焰 slide 12-13）。
- **独立验证的独立发现**：星空有云在赛前对 10 个开源框架做的共性归纳里，"独立验证结果（另一角色或流程复核证据链）"被列为五条规律之一（S2·星空有云 slide 5），与 LuaN1ao 的图写入校验、我们自己的权威签发相互印证。

判读：目标平台用 flag 判定，验证体系不直接得分；但"证据引用＋写入校验"是团队自己相信结果、以及赛后复盘的前提。red-harness 的权威签发与证据引用在这层领先（见 [zc-architecture-comparison.md](zc-architecture-comparison.md) 第 4 节），本节只登记对手的同层实践与可借鉴的复盘面板。

## 4. 设计理念谱系：六个有争议的问题

两届头部方案在"工程该做多少"上没有共识。把答辩材料的主张抽出来，能看到六个真正的分歧点；每个分歧都能找到至少一个高分反例，这正是本文与能力对比文档的差别所在。

### 4.1 控制：补能力缺口，还是补信任缺失？

- **约束派（多数）**：BreachWeave 的出发点是"智能决策要有工程边界——LLM 负责规划、判断和调度，所有动作必须通过受控工具、资源上限和生命周期约束落地"（S2·BreachWeave slide 5）；Sniper 用 8 层中间件在工具调用的每个环节做校验与修复（S2·Sniper slide 11）；奇盾明焰把"从依赖 Agentic Loop 自主迭代，到通过 Harness 设计持续约束、校准 Agentic Loop"作为标题（S2·奇盾明焰 slide 9）；京东在"减法哲学"下仍保留中央调度器、工具网关与安全熔断（S2·ToBeNumberOne slide 5-7；复盘文）。
- **把口号写进目录的约束派**：戴夫的后花园的目录直接是三句话——"不相信大模型的自制力"、"知识驱动攻击"、"代码约束>提示词约束"；实现上由事件驱动引擎保证每一轮调用都经过完整管控链（"这条管控链对大模型完全不可见"），再加独立会话的顾问在检测到打转时纠偏（S2·戴夫的后花园 slide 3、5-6、9）。
- **留白派**：Cairn 把 Harness 压到最小——黑板＋Dispatcher＋装满工具的容器，不预设流程、角色与知识库（`Cairn/README.md` 第 20、124 行；复盘文）；For future 认为上一届的 DAG 硬约束"伤害推理空间"，改用自然语言边＋FSM（S2·For future slide 11）。
- 两派的分界不在"要不要控制"，而在**控制什么**。Cairn 作者在复盘文里把控制分成两类：模型真的不会做（补能力缺口，必要）与"模型其实会做、但不确定它会按我想的方式做"（补信任缺失，可能随模型进化变成冗余）。他引用 Anthropic《Scaling Managed Agents》中"Harness 会编码开发者对模型能力不足的假设、需要持续重新审视"的判断作为支撑。这是二手观点，但与结果吻合：最激进的留白派拿到唯一 AK，同时约束最重的两家占据第一、第二。
- **对 red-harness**：我们目前只强制授权范围、预算、破坏性动作边界与验证权威，推理层不设约束（[agent-architecture-v2.md](agent-architecture-v2.md) 第 2 节）；这个"只控信任与资源、不控思路"的位置有直接赛果证据支持。

### 4.2 状态：模型回忆，还是系统资产？

这是共识最强的一条。复盘文的归纳是"状态必须成为系统资产，而不是模型回忆"；对应实现：BreachWeave 的 Idea/Memory 双层状态板（S2·BreachWeave slide 12）、Cairn 的 Fact/Intent/Hint 事实图（README 第 55-63 行）、运行完成的全局黑板（主目标事实、状态变更、能力矩阵、有效发现、路径进度、尝试记录、失败方向、私有笔记）与每个子 Agent 的"分支黑板"（S2·运行完成 slide 5）、京东的笔记机制与 Agentic C2 GlobalState（S2·ToBeNumberOne slide 7、9）、天翼的 Memory 中枢（S2·Sniper slide 12）。星空有云先做了一轮跨框架归纳，把"中间状态持久化"列为开源框架的共性规律，然后才落到 File-as-Bus 的实现（S2·星空有云 slide 5、7）。

分歧只在存储介质：文件（BreachWeave、SickHackShark、奇盾、llmnor）、图（Cairn、LuaN1ao、BinX）、数据库（TPT-Agent 的 SQLite、CyberStrikeAI）、运行时共享内存（S1 ai小分队笔记本、D@WNEDG3 的跨 Agent 关键信息共享）。介质不是胜负手，**"低噪音、可决策"的投影**才是：BreachWeave 直接写"长任务推进到后期，真正失真的往往不是能力，而是状态本身"（slide 12）。

对 red-harness：世界投影＋notes 目录与这条共识同向；我们在阶段一重启时生成接力简报，正是"低噪音投影"的一种确定性实现。

### 4.3 多 Agent：分工，还是并发？

- **分工派**：Sniper 的 DeepAgent 让主 Agent 保留完整上下文、子 Agent 专注子任务以节省主上下文（slide 10），卡点时召唤"专家 Agent"提供独立上下文的突破思路（slide 18）；CHYing 的 Orchestrator＋Browser/C2/Reverse 子 Agent 按能力与工具可见性隔离（`CHYing-agent/README.md` 第 56-95 行）；hackathon-pentest 的 Web/内网专家（`README.md` 第 15-30 行）；H-Pentest 的 Meta/Strategic Supervisor＋Worker（`README.md` 第 27-42 行）。
- **并发派**：Cairn 的 Worker 能力完全相同、没有角色，存在意义是同时探索不同分支（复盘文；`server-protocol.md` 第 21 行的任务式指挥类比）；S1 ai小分队"多 Agent 独立解题＋共享笔记本弱耦合协作"（slide 6-7）；D@WNEDG3 用 5 个同质 Agent、不同温度竞争同一道题（slide 9）。
- 判据是任务类型：流水线任务适合固定角色，探索性任务适合并发＋共享状态（复盘文）。RedCortex 走中间路线：固定大流程图上做决策路由、专家池执行（`RedCortex/README.md` 第 32-37 行），并明确它面向的是靶场与竞赛环境。
- **对 red-harness**：同题多会话竞争是"并发派"的最小实现（PLN-04 已落地）；"专家咨询"这类分工可以落到 Agent 侧的多会话 Prompt 组合，不必把角色编排写进 Harness——v2 已把角色编排列为非目标。

### 4.4 知识：注入语料，还是留白给模型？

3.4 已给出两派证据；理念层的补充是 For future 的假设——"大模型已经具备所有关于渗透的通用专家知识，且知识储备远超任何个人"（S2·For future slide 8；复盘文），因此零领域知识是**主动排除**而不是遗漏。反过来，Sniper 的结论是"Agent 的上限就是知识工程的边界"（slide 21），BreachWeave、NeuroSploit、SickHackShark 都用大体积语料加固长尾能力。

两派都没错，前提不同：留白派赌模型能力继续上升、控制权交给模型；注入派赌长尾 payload 与产品知识模型不会现查、把确定性的知识固化成资产。对 red-harness，这是内容与 Agent 侧决策：技能与知识目录保持只读供给（ZCP-07 已落地），选择与排序不进入 Harness。

### 4.5 意图与控制权：Prompt、Context、Intent 三代论

- Antix 提出三代 AI 工程：第一代面向模型（Prompt Engineering）、第二代面向 Agent（Context Engineering）、第三代面向意图（Intent Engineering）；意图工程拆成意图的理解、表达、执行三段，Meta-Tooling 属于执行段的补充。同时判断"通用 Agent 的崛起会抹平 Agent 框架的壁垒"（S1·Antix slide 7-8、18-19）。
- 长亭外："Programming, not Prompting"——用 `<TASK>/<WHILE>/<IF>` 这类结构化模板表达规划，并自研 MPI（Model Programming Interface）作为中间表示（S1·长亭外 slide 5-10）；公开报道补充了"探索-规划-利用三阶段、最高 60+ 路径并行"的工程细节。
- Cairn 反过来：不给任何流程，Agent 只有三种任务指令，路径从黑板涌现（S2·Bytex slide 13-14）。
- 三家的共识是**控制权从 Prompt 软约束迁移到代码、协议与反馈回路**（复盘文对控制层的总结）；分歧是"编程"发生在 Harness/编排层（长亭、aide）还是由模型在协议内自组织（Cairn）。
- **对 red-harness**："可编程"只做在强制层（预算、止损、生命周期），不做领域 DSL；这与 v2 的非目标、以及 `tests/test_architecture.py` 对已删子系统的防守一致。

### 4.6 人与机器：从"人机协同"到"人类是架构师"

S1 yhy 的 Human-AI Teaming 把顾问 Agent 设计成"从不盲信第一次回答"的第二视角（slide 5、13）；D@WNEDG3 把分级提示当成调度资源，30 步未解就注入提示并换队列（slide 9）；Cairn 把 Hint 定义为随时可注入的外部判断，并类比"任务式指挥"里的指挥官意图（`server-protocol.md` Hint 与设计渊源小节）；京东用 "Less Tech, More Business" 把业务理解列为核心（S2·ToBeNumberOne slide 5）；CyberStrikeAI 提供 HITL 审批、工具白名单与可追溯决策（README 第 128 行）；运行完成的展望提出"人机精准协同"（slide 14）。

比赛规则禁止人工直接操作靶机，反而把人的角色推到了意图、约束与知识供给层——这与"人类是架构师，AI 是专家"（S2·For future slide 10）是同一种判断。

## 5. 两届演进：从"跑通单题"到"长链路收敛"

把两届放在一起，能看到五个方向的变化。同源团队跨届的选择变化，比分届统计更能说明问题。

### 5.1 命题：单题闭环 → 长链路收敛

首届赛题来自 XBOW benchmark，核心命题是"Agent 能不能稳定跑通单题解题闭环"；第二届换成四赛区闯关（识器/洞见/执刃/铸剑）＋计分衰减，命题变为"能不能在长链路、多阶段、强不确定环境中持续推进并最终收敛"（复盘文《两届TCH之后》）。

直接后果是三层同时升级：状态从笔记升级为事实图/双层看板/Path Map；控制从重试升级为 Observer/Manager/调度器/熔断；行动从工具调用升级为网关/沙箱/隧道/C2。这也是"Harness Engineering"成为第二届几乎所有 Top 方案重心的原因（复盘文）。

### 5.2 底座：Agent 框架 → 通用 Agent＋自建运行时

- 首届大量方案基于通用框架：newmapta 用 CrewAI（README 第 5-7 行）、Cruiser 用 langchain、SickHackShark 用 langchain-deepagents（README 第 5 行）、H-Pentest 自建 FastAPI 平台、CyberStrikeAI 用 Go/Eino 编排。
- 第二届时头部方案的底座转向"通用 coding agent＋自建 runtime/沙箱"：aide 的定位就是"通用 Agent 运行时＋场景 overlay"（README 第 24 行）；BreachWeave 建在 Pi SDK 上（`ARCHITECTURE.md` 的分层图）；Cairn 把 Claude Code/Codex/Pi 当可替换 driver（README Supported worker backends）；CHYing 从 langchain 迁到 Claude Agent SDK 并做源码级改造（README 第 66-74 行）；京东自述 "One Agent, All Scenarios"（slide 5）。Antix 首届的预判——"通用 Agent 的崛起会让 Agent 框架的技术壁垒消失"——在第二届被验证（S1·Antix slide 7、19）。
- 赛后仍在演进：LuaN1aoAgent 的 v2 是赛后重写（Planner-Executor-Observer、因果图、内容寻址工件、per-Task Docker），其 README 明确"v1 的 benchmark 成绩不自动归因于 v2"；本文引用的是赛后代码，不是比赛版本（见第 7 节）。

### 5.3 同源分叉：同一支技术血脉的两种答案

最清晰的对照组来自 chainreactors 系谱：

- S1 Antix（第 4 名）用 tinyctfer：100 行 runtime、Meta-Tooling、零调优（`tinyctfer.py` 第 1-19 行；README 第 5 行）。
- S2 Bytex（第 3 名）用 Cairn：答辩 PPT 作者页写明 "TCH 第一届第四名[Antix]"（S2·Bytex slide 2），路线转向无角色黑板与最小 Harness。
- S2 For future（第 7 名）用 aide：README 直接写"上一届方案在 tinyctfer，这一届分裂出两条路线：Cairn / aide"（`aide-for-pentest/README.md` 第 11-16 行）。
- 结果是同一技术在两个方向上都进前七：一条把工程压到最小、让模型在协议内自组织；一条把通用运行时＋overlay 做成可迁移底座。这比"多 Agent 好还是不好"更有信息量：**胜负手不在架构堆料量，而在状态、上下文、控制三环是否闭紧。**
- CHYing 是另一个跨届样本：首届第 9，第二届主赛场第 60、零界赛场第 1（`CHYing-agent/README.md` 开头），同届内完成底座迁移。说明单次名次与模型/赛制强耦合，不能用一次成绩给架构下结论。

### 5.4 速度与成本：第二届的新变量

- 计分衰减把速度变成架构指标（首解 +20%、31 名后 -80%，见 CHYing README 与 For future 复盘文）。头部方案的对策是"并发即探索"：BreachWeave 默认最多 7 个 Solver（`manager.ts` 第 57 行）、D@WNEDG3 每题 5 个 Agent（slide 9）、长亭 60+ 路径并行（二手报道）、Sniper 把前置侦察做成确定性管道以节省 2-5 分钟与 token（slide 10）、For future 用外层 `/loop` 五分钟节拍选题派发、内层多 worker 并行（slide 5）。
- 成本成为一等变量：首届模型受限、成本低（S1 ai小分队 1057.8 元、平均 13.22 元/题，slide 12；tinyctfer 约 1500 元，`tinyctfer.py` 第 12 行）；第二届放开模型后出现"多模型并行＋按性价比分工"（BreachWeave slide 17 的模型战绩面板；Cairn 复盘文自述 token 消耗在十强中最少、且是前十中唯一 LLM 消息数为数十万级的队伍）。
- 与 red-harness 的对应：题间并行 ≤3、题内多会话预算均分（[agent-engineering.md](agent-engineering.md) 第 3 节）已在这条线上；ZCP-01/02/04（接入面、限流与实例上限、分级提示）是速度经济的前置门，未落地前不产生分数。

### 5.5 知识工程：从"有没有库"到"会不会泛化"

首届的知识供给是"把资料放进容器"；第二届出现两条路：BreachWeave/Sniper 把知识做成技能包＋指纹映射＋自蒸馏，Cairn 把知识全部砍掉；运行完成走了居中路线——500+ 预构建知识库、12 类漏洞专项知识，同时用全局/分支黑板维持多路径协同（S2·运行完成 slide 5）。天翼的密码案例（`weaver@2024` vs `weaver@2023`）证明关键变量是表示方式：字面值只能照抄，模式（"weaver@年份"）才能枚举（S2·Sniper slide 21）。BinX 首届已经把 RAG 客户端封装成 Executor 可直接调用的原子工具，把"查资料"变成可调度技能（S1·BinX slide 18），这是第二届知识工程的前身。

判读：第二届真正升级的不是知识量，而是知识的表示与调用时机——检索按需、模式可泛化、工具按技能加载，而不是全量注入。

## 6. 对 red-harness 的启示

本节只回答"理念层该保持、试验、拒绝还是归 Agent 侧"；代码级差距、量化验收与排期仍以 [zc-architecture-comparison.md](zc-architecture-comparison.md) 和 [agent-engineering.md](agent-engineering.md) 为准。不新增 ZCP 编号、不改任何 roadmap 状态。

### 6.1 结论总表

| 结论 | 处置 | 对手证据（摘要） | red-harness 落点 |
| --- | --- | --- | --- |
| 状态外置＋低噪音投影 | 保持（已实现） | BreachWeave 双层状态板；Cairn Fact/Intent；运行完成 Path Map | `src/harness/world/context.py`（第 288-301 行 Ruled out 投影）；ZCP-08 已落地 |
| 会话级上下文经济学（工件化/压缩/回注） | 试验（阶段二待复核） | BreachWeave 三层管线；Sniper 8 层中间件；奇盾笔记 Agent；S1 ai小分队重启回注 | ZCP-05 阶段一已落地；阶段二＝容器内 Pi 扩展，属执行边界变更 |
| 控制从 Prompt 迁到代码/协议 | 保持 | Observer、Dispatcher、Ralph-Loop 的硬约束 | `solver_policy.py`、`budget.py`、runtime 生命周期 |
| 结束判定外置 | 保持 | BreachWeave Ralph-Loop；Cairn goal Fact | `solver_policy.decide_solver_action` 纯止损律 |
| 超时后收尾兜底 | 保持＋小步试验 | Cairn `conclude_timeout` 90s 双阶段 | ZCP-10 已落地；可评估会话级收尾模板复用 |
| 并行＝并发而非分工 | 保持 | Cairn 同质 Worker；BreachWeave 最多 7 Solver；D@WNEDG3 每题 5 Agent | `runtime/executors.py`（第 137 行，每题上限 4 会话）＋题间 ≤3 |
| 知识以只读 markdown 按需供给 | 保持＋扩充 | 多数队伍 KB/Skill；CHYing 关键词 wiki | `skills/offensive/`、`knowledge/`；ZCP-07 已落地 |
| 证据与权威签发 | 保持（领先） | LuaN1ao 图写入校验；CyberStrike 审计；天翼证据包 | `verification.py`（第 71-76 行：verified 必须有证据）、`runtime/evidence.py` |
| 恢复与幂等对账 | 保持（领先） | Cairn 租约心跳；CyberStrike runlease | `runtime/checkpoint.py` 对账与锁 |
| 用量归因与复盘面板 | 保持＋试验 | BreachWeave 模型/Prompt 战绩；LuaN1ao trace；奇盾案例回放 | ZCP-09 已落地；可评估人类可读时间线 |
| 黑板/蚁群/角色编排/FSM/向量 RAG 进核心 | 拒绝 | Cairn/aide/CHYing/CyberStrike 各自实现 | v2 非目标（[agent-architecture-v2.md](agent-architecture-v2.md) 第 1 节）；`tests/test_architecture.py` 防线 |
| 单一 Meta-Tooling 入口、MCP 泛滥、无界并发 | 拒绝 | Antix 元工具；yhy 反 MCP 堆砌；各队显式并发上限 | Gateway 最小工具面＋既有并发上限 |
| 对抗性第二视角/专家咨询 | 归 Agent 侧 | yhy 顾问；CHYing advisor；Sniper 专家 Agent；BreachWeave Observer | 同题多会话＋Prompt/overlay；Harness 不做角色编排 |
| 零知识留白 vs 语料注入 | 归 Agent 侧 | For future 零领域知识；BreachWeave/Sniper 大语料 | `skills/`、`prompts/` 的内容决策；Harness 不解析不排序 |
| 速度经济（接入面/限流/实例/提示阈值） | 赛前门（保持既有状态） | For future 外层 `/loop`；计分衰减 | ZCP-01/02/04 未落地，状态见 agent-engineering.md |

### 6.2 保持：已经对齐的部分不再加机制

- **信任与认知的分工**：我们只强制授权、预算、破坏性动作边界与验证权威；对手顶级方案的"留白派"（Cairn）与"约束派"（BreachWeave/Sniper）都取得了前列名次，说明这个位置本身不是劣势；星空有云的"薄控制＋厚状态"（slide 8）是同一分工的独立表述。需要抵抗的诱惑是"把对手的状态板/观察者/调度器搬进 Harness 核心"——那会重新引入认知层代码。
- **状态投影**：世界投影只给事实视图（含失败记录），不提供候选动作与计划；ZCP-08 已把 refuted/superseded 放进 Ruled out（`src/harness/world/context.py` 第 288-301 行）。保持"只投影、不排序"。
- **硬控制与结束判定**：止损律与预算超限收尾是纯函数/纯会计，和两届复盘"控制必须从 Prompt 软约束迁移到代码、协议与反馈回路"的结论同向，也是我们相对对手的工程优势。
- **并发模型**：同题多会话竞争＋题间 3 路已是头部方案的同量级（对手 5-7 并发是在平台实例上限与自建资源下取得的结果）；平台实例上限仍为 3，本机按 4GB/4CPU 每会话估算（agent-engineering.md 第 4 节），没有证据支持提高上限。
- **证据与恢复**：verified 必须有独立证据、checkpoint 对未决动作对账，比对手多数方案更严格；LuaN1ao 的图写入校验与 CyberStrike 的 runlease 是唯一同层实践，保持即可，不必扩大。
- **归因与复盘**：按 (prompt, provider, model) 的 `executor_summary`（ZCP-09）与 BreachWeave 的战绩面板同层；先把它用在选型与赛后复盘，不引入新机制。

### 6.3 试验：需要补一手证据再决定

- **T1 上下文经济学阶段二（容器内钩子）**：对手把"大输出落盘＋压缩＋重启回注"做在 Agent 运行时内部（BreachWeave `large-tool-result.ts`/`pentest-compaction.ts`；Sniper 中间件；奇盾笔记 Agent）。我们只有会话级重启（ZCP-05 阶段一）。试验方式：按 [zc-improvement-design.md](zc-improvement-design.md) 的 C 段流程走 maintainer 复核，评估 Pi 扩展钩子的安全边界与收益；基线保持会话级重启。验收看"同等预算下有效工具调用与提交数"，不看单题戏剧性案例。
- **T2 会话级收尾模板**：Cairn 的 `conclude_timeout` 在超时后用同一 session 产出 Fact，我们对应的是 ZCP-10 的结构化摘要。可评估把"四段固定标题＋缺失可观测"复用到会话重启/取消路径，前提是不新增验证权威、不把摘要当证据。
- **T3 人类可读复盘时间线**：LuaN1ao 的实时 trace、CyberStrike 的攻击链回放、BreachWeave 的战绩面板说明复盘产能是隐性竞争力。试验方式：`harness report`/`harness audit-run` 增加从权威 trace 渲染的时间线（只读、无新存储），不引入 Web UI。
- **T4 接力简报质量观测**：当前简报由世界投影生成，风险是投影稀疏导致简报变薄（zc-improvement-design.md 第 8 节已提示）。试验方式：在 `result.json`/`solver.summary_*` 旁新增会计级指标（简报词数、notes 条数、段落缺失），只做观测；Harness 不排序、不改写内容。

### 6.4 拒绝：不把对手的认知层搬进 Harness

- **黑板/蚁群/角色编排/FSM DSL/向量检索**：这些在 Cairn、aide、CHYing、CyberStrike 里各自成立，因为它们是那支队伍的**整个系统**（协议、调度、执行都在自己手里）；red-harness 的定位是通用 coding agent 外的环境/信任/记账/恢复层，把它搬进核心既违反"认知归 Agent"，也会触发 `tests/test_architecture.py` 的已删子系统防线。需要时用 prompt/overlay/知识目录表达（如把 ralphloop 当提示模式），不引入解析器与执行器。
- **强制单一 Meta-Tooling 入口**：tinyctfer 的威力来自沙箱与 Python 工具集（Agent 侧设计），不是来自 Harness 规定"只能有一个工具"。保持 Gateway 最小工具面与可插拔 executor，不把单入口写成强制契约。
- **MCP/工具堆砌**：yhy 答辩明确反对把每个工具封装成 MCP（工具描述吃上下文）；我们已有最小工具面，保持现状，不为"生态"扩大默认工具集。
- **无界并发与产品化子系统**：C2/WebShell/K8s/Redis/RBAC/向量库是产品能力，不是得分必要条件；两届头部方案也没有一致依赖它们。继续维持模块化单体边界与显式资源上限。
- **计分驱动的 Harness 行为**：For future 的外层调度（按积分表选题、取 hint、管实例）成立是因为它被放在 Agent/运维层。red-harness 保持评分无感，只暴露接入面与安全阀；选题与提示策略留在赛前运行手册与 Agent 侧。

### 6.5 归 Agent 侧：只动 Prompt / Overlay / 知识内容

- **对抗性第二视角与专家咨询**：yhy 的顾问、CHYing 的 advisor、Sniper 的专家 Agent、BreachWeave 的 Observer 都是"多一个独立上下文看问题"。我们已支持同题多会话，正确做法是用不同 system prompt/模型组合出"主攻＋顾问/观察者"的会话，而不是在 Harness 写角色编排。
- **零知识 vs 语料注入**：这是内容策略。留白（For future）与注入（BreachWeave/Sniper）都有高分样本；我们的技能/知识目录已经具备只读挂载与索引（ZCP-07），下一步是扩充内容并标好来源与许可，不是加检索逻辑。
- **业务场景建模**：xjtuHunter 的"页面探索→场景塑造→漏洞检测"、京东的"More Business"都属于模型侧推理与提示工程；世界投影只提供事实，不替 Agent 做场景归纳。
- **经验蒸馏与失败复用**：NeuroSploit 的 ReMem、Sniper 的自蒸馏、CHYing 的 retry-handoff、LuaN1ao 的因果图记忆都是 Agent 把历史写成可复用资产。我们提供 notes 目录与简报归档即可，解析与排序不进 Harness。
- **上下文触发阈值**（30k/100k、连续 20 次工具调用、同题 N 次清笔记）：这些都是 Agent 运行时配置；只有在 T1 通过执行边界复核后，才以 Agent 侧钩子形式出现。

### 6.6 待 triage 候选（无编号，不改状态）

| 候选 | 目的 | 对手证据 | 前置条件 |
| --- | --- | --- | --- |
| C1 接力简报质量观测 | 防止"重启了但简报变薄" | S1 ai小分队回注、BreachWeave 压缩保留清单 | 无；纯会计指标 |
| C2 人类可读复盘时间线 | 提升复盘产能（不直接得分） | LuaN1ao trace、CyberStrike replay、BreachWeave 战绩面板 | 只读渲染，不新增权威工件 |
| C3 会话级收尾模板复用 | 把 ZCP-10 扩到重启/取消路径 | Cairn conclude 双阶段 | 与 ZCP-05/10 边界核对，不产生证据 |
| C4 成本字段完整性 | 支撑模型选型与预算策略 | BreachWeave 模型战绩面板、S1 成本看板 | provider 提供成本数据时启用 |
| C5 知识语料扩容清单 | 补齐 payload/产品知识长尾 | BreachWeave 226 md、Sniper 指纹映射、SickHack 三库 | 许可与 pinned 取数流程 |

## 7. 未证实与不可比

- **静态阅读边界**：只证明"材料里有什么"，不证明"场上有效"；不运行对手代码，不下"谁更强"的结论。全部仓与 PPT 以 2026-10-16 的 pin 为准，上游继续演进后行号会漂移。
- **自述成绩**：排名、解题数、成本、token 数均来自官方材料或队伍自述——如 BreachWeave 1/613、Cairn 54/54 唯一 AK、Sniper 51/54 第二、For future 50/54 与"#42→#4"消融叙事、S1 长亭 94% 成功率与 60+ 路径——本文未复现、不背书。
- **数据口径**：第二届队伍数存在 610（Cairn README/复盘文）与 613（BreachWeave README）两种口径；成本数字的模型、价格与统计口径不同，不可横向比较。
- **材料缺失**：第二届 2/4/5/6/8/9/10 名无公开代码，只有答辩 PPT；首届 1/2/5/6 名无公开 Agent 代码（第 6 名只有评测与平台侧仓）；两届 Top20 内还有若干名次无任何公开材料（见 1.1、1.2 的说明）。图像型 PPT 经本机 OCR 转写后引用，个别字词可能有识别偏差，引用以语义为准。
- **版本漂移**：LuaN1aoAgent 为赛后 v2 重写，v1 成绩不自动迁移（其 README 明示）；CyberStrikeAI 的 HEAD 在对比文档 pin 之后有新提交（本文按 `63f3797e` 记录）；CHYing、Cruiser 等仓的 README 同时包含两届战绩。
- **身份同源**：chainreactors 系谱（Antix→Bytex、tinyctfer→Cairn/aide）依据答辩 PPT 作者页、仓内署名与复盘文自述整理，未做独立核实。
- **材料许可**：PPT 与文章只做摘述与链接，不整段复制；官方仓的免责声明同样适用于本文引用。

## 8. 参考

官方资源：

- [Yeti-791/Tsec-Hackathon](https://github.com/Yeti-791/Tsec-Hackathon)（两届资源仓，pin `3d380f32ca21`）：两届答辩 PPT、开源仓导航、赛事资料。
- 赛事页 [zc.tencent.com/hackathon](https://zc.tencent.com/hackathon)；平台 [challenge.zc.tencent.com](https://challenge.zc.tencent.com/)。

公开复盘文章（二手材料，选取与本文直接相关者）：

- 《两届TCH之后——AI 渗透测试 Agent 的 Harness 工程演进、防御与我的思考》——状态/行动/控制三层归纳、控制与信任的讨论、多 Agent 分工与并发的判断。
- 《无径之径：Cairn AI 从渗透测试到通用问题的求解》与《国内最强 AI 渗透测试 Agent——TCH 唯一 AK 战队复盘》——Cairn 黑板的来龙去脉与赛果自述。
- 《Less Than Nothing——从本质的本质出发，AI攻防的另一条路》——For future 的消融实验、控制论隐喻与零领域知识主张。
- 《渗透成功率超94%！长亭科技 AI 自主渗透智能体再获“腾讯云黑客松”决赛冠军》——首届冠军的公开工程数字。

开源仓与 pin：见第 1 节两张索引表；官方导航之外补充的 [TPT-Agent](https://github.com/CloudEver-Team/TPT-Agent)、[RedCortex](https://github.com/CloudEver-Team/RedCortex) 亦列于表中。

本仓相关文档：

- [zc-architecture-comparison.md](zc-architecture-comparison.md)：代码级能力差距与 ZCP-05..11。
- [zc-improvement-design.md](zc-improvement-design.md)：A/B/C 段实现设计与验收矩阵。
- [agent-engineering.md](agent-engineering.md)：赛场落地、ZCP 梯子与 P0/P1。
- [agent-architecture-v2.md](agent-architecture-v2.md)：Agent-Native Harness 的当前设计与非目标。
- [tsec.md](tsec.md)：TSec 集成与消融矩阵；[roadmap/README.md](roadmap/README.md)：子系统 roadmap 索引。
