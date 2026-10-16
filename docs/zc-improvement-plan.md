# red-harness 改进计划：≤5 工作日，先得分后工程

> 依据：[zc-philosophy-comparison.md](zc-philosophy-comparison.md) 第 6 节（理念层取舍）、
> [zc-improvement-design.md](zc-improvement-design.md)（A/B/C 段设计）、
> [agent-engineering.md](agent-engineering.md) 第 5 节（ZCP 状态权威）。
> 定位：只排"下一届多拿分"的最小可执行集、排期与验收口径；实现细节以设计文档为准，
> 状态以 [agent-engineering.md](agent-engineering.md) 与 [roadmap/README.md](roadmap/README.md) 为准。
> 边界：本计划自身不改代码、契约、schema 与执行边界；ZCP-05 阶段二涉及执行边界，
> 按 [security/execution-boundary.md](security/execution-boundary.md) 先复核后实施。
> 窗口：≤5 个工作日（D1-D5）。先固化（G0）→ P0 提分门 → P1 低风险候选 → 联调日证据。

## 1. 现状（2026-10-16 快照，归并 origin/main 之前的记录）

> 本节是计划立项时的快照，保留原样以供对照；ZCP 与 roadmap 的当前状态一律以
> [agent-engineering.md](agent-engineering.md) 第 5 节与 [roadmap/README.md](roadmap/README.md) 为准。
> 归并 `origin/main`（`4349a22`，操作者提示通道 `harness hint` + 控制脊瘦身）后，ZCP-02/04
> 已落地、ZCP-05 阶段二已获批落地（默认关闭），本节列出的"未落地"记录已过期。

**ZCP 梯子**（状态权威见 [agent-engineering.md](agent-engineering.md) 第 5 节）：

- 未落地：ZCP-01（接入面核对）、ZCP-02（3 req/s + 实例 ≤3）、ZCP-04（阈值分级提示）。
- 已落地：ZCP-03、ZCP-06..11；ZCP-05 阶段一已落地，阶段二（容器内 Pi 扩展）待 maintainer 复核。
- 结论：真正的提分缺口集中在接入面、限流与提示经济、上下文经济学阶段二。

**roadmap 的 `[~]` 项**（RUN-02/04、EXE-01/02/03/05/06、BEN-02、WLD-05、QA-04）全部是"实现已完成、缺 Docker/CI 实测证据"。
它们不进本窗口的 P0，只在联调日（G3）顺带回收证据；没有证据的行保持 `[~]`。

**工作树**：A/B 段实现（`src/harness/solver_loop.py`、`src/harness/progress.py`、
`src/harness/world/context.py`、`src/harness/benchmark/runner.py`、
`src/harness/benchmark/tsec_adapter.py`、`src/harness/cli.py`、知识库与测试）以及
[zc-philosophy-comparison.md](zc-philosophy-comparison.md)、[zc-improvement-design.md](zc-improvement-design.md)
仍未提交。最近一次本地全量验证为 `pytest -q` 283 passed / 2 skipped（Docker 与 Pi 路径未在本机实测）。

**平台侧**：ZCP-01 需要真实平台凭据、官方 `/api` 文档与 Docker 环境；本机尚未打通。
联调日之前，所有接入面结论都只能是离线契约，不能写成"已对齐"。

## 2. 目标与验收口径

- 目标（得分优先）：把直接影响得分的能力补齐到"可离线验证＋可在联调日实测"；工程项只保留阻断比赛的依赖。
- 成功标准：G0 两个 PR 合入且 CI 绿；ZCP-01/02/04 离线验收全绿且联调日完成实时核对或按 blocked 如实记录；
  ZCP-12/14 落地；ZCP-05 阶段二复核包提交，获批且窗口允许时最小实现落地。
- 效果验收：只有下一届比赛的得分/名次是效果验收；离线测试与联调证据只证明"可运行、可复现、可对账"，不折算提分。
- 状态更新规则：只有带验收证据的 PR 才能把 ZCP 行从"未落地"改为"已落地"；联调缺失时保持未落地或标 blocked。

## 3. 工作流总表

| ID | 优先级 | 交付摘要 | 工期预算 |
| --- | --- | --- | --- |
| G0 | 前置 | 固化现状：PR-1 整合 A/B 段与知识库，PR-2 本计划与编号行 | 0.5-1 天 |
| ZCP-01 | P0 | 接入面离线契约＋preflight 诊断＋实时核对清单与证据模板 | 1 天 |
| ZCP-02 | P0 | 3 req/s 令牌桶、实例 ≤3、close 释放守护与对账 | 1 天 |
| ZCP-04 | P0 | 阈值分级提示（默认关闭、一次触发、扣分可对账） | 0.5-1 天 |
| ZCP-05 阶段二 | P0 复核 | 复核包；D3 末审批门，获批后最小实现（默认关闭） | 0.5 天＋≤1 天 |
| ZCP-12 | P1 | 接力简报质量观测（会计级指标） | 0.5 天 |
| ZCP-14 | P1 | 知识语料扩容第一批（pinned 取数） | 1 天 |
| ZCP-13 | P2 条件 | 会话级收尾模板复用；仅当 D3 门按期时启动 | 0.5 天 |
| G3 联调日 | P0 证据 | ZCP-01 实时核对＋EXE-01/02、BEN-02 证据 | 1 天 |

### G0 固化现状

- 交付：PR-1 一次整合当前工作树（A 段 ZCP-11/05 阶段一/06/07 与 B 段 ZCP-08/09/10、知识库、测试与文档），
  提交历史按 ZCP 子项分组；PR-2 新增本计划并在 [agent-engineering.md](agent-engineering.md) 第 5 节登记 ZCP-12/13/14。
  A/B 段共享 `src/harness/solver_loop.py`、`src/harness/benchmark/runner.py`、`src/harness/cli.py`，
  hunk 级拆分的成本高于收益，因此合并为一个工作流域 PR、子项分提交。
- 验收/证据：两个 PR 均通过 CI 同款 namespace 扫描、`ruff check .`、`pytest -q` 与
  `pytest tests/test_docs_consistency.py -q`；hosted CI 绿；PR-1 在描述里说明不拆分的原因。
- 依赖：无。
- 停止条件：CI 或 docs 一致性未绿前不启动 D2 的新工作。

### ZCP-01 接入面核对

- 交付：① 在 `tests/test_tsec_adapter.py` 补齐 list/start/submit/hint/close 的离线契约
  （含认证头常量断言与错误分支）；② `harness preflight --tsec` 给出 base URL、
  token env（默认 `BENCHMARK_TOKEN`）与 SDK 可用性的可操作诊断；③ 产出一页实时核对清单：
  端点、方法、认证头、请求/响应样例、需记录的证据字段。
- 验收/证据：离线契约全绿；preflight 在缺失凭据时给出可操作错误；联调日在真实平台跑通五项调用，
  把端点差异与认证头记录进 PR 与 run-dir；证据不足时在 [agent-engineering.md](agent-engineering.md)
  标 blocked 并写明缺什么。
- 依赖：平台凭据与官方文档；联调环境（D5）。
- 停止条件：若官方 `/api` 与 SDK `openapi/v1` 存在实质差异，先记录差异与影响面；
  改动 `src/harness/benchmark/tsec_client.py` 需 maintainer 复核后另开 PR。

### ZCP-02 限流与实例上限

- 交付：在 `src/harness/benchmark/tsec_client.py` 与
  `src/harness/benchmark/tsec_adapter.py` 对每次平台调用套 3 req/s 令牌桶；
  维护实例注册表（start 计数、close 递减、超时回收对账），硬上限 3；
  429 退避后 fail-closed，不无限重试；所有终态路径都调用 `close_challenge`。
- 验收/证据：用假时钟的离线计数测试证明不越界；实例泄漏对账为零；
  `tests/test_tsec_adapter.py` 覆盖 429 退避与关闭幂等；联调日记录真实请求节拍与实例数。
- 依赖：ZCP-03 的并行信号量（已落地）。
- 停止条件：若 SDK 不暴露可拦截的调用边界，退回 adapter 方法级包装；不得绕过限流换速度。

### ZCP-04 阈值分级提示

- 交付：新增 `--hint-threshold N`（默认 0＝关闭，保持现状），按步数或无效进展阈值对单题触发一次提示；
  只对已启动实例取提示；保留 `--hint` 作为显式人工覆盖；`result.json` 记录 `hint_used` 与
  10% 扣分的对账字段。
- 验收/证据：`tests/test_tsec_adapter.py` 覆盖阈值只触发一次、失败不重试刷屏、跨题隔离；
  联调日记录一次真实取提示的得分影响。
- 依赖：ZCP-02 的实例注册表（同一状态对象）。
- 停止条件：平台提示接口不可用时保持关闭并记 blocked；提示缺陷不得靠多次拉取规避。

### ZCP-05 阶段二复核包（执行边界门）

- 交付：复核包六件套——威胁模型增量、hook 范围（仅 `tool_call` 与 `session_compact` 两个）、
  数据流与落盘位置（Agent 工作区内、带 preview 上限）、回退开关（默认关闭）、
  消融方案（同种子同题集有钩子 vs 无钩子，比重启次数、工具调用、提交数）、
  [security/execution-boundary.md](security/execution-boundary.md) 更新草案。
- 验收/证据：D3 末提交复核包并得到 maintainer 明确答复；获批且窗口允许时，
  在 D4-D5 做最小实现（两个 hook 上限、默认关闭）并给出测试与消融结果；未获批只保留草案，不写代码。
- 依赖：maintainer 复核时间；G0 已合入。
- 停止条件：任何否决或未答复都视为不实施；回退方案是增强会话级重启简报（阶段一＋ZCP-12）。

### ZCP-12 接力简报质量观测

- 交付：每次重启记录会计级指标——简报字符数、来源 notes 条数、缺失段落列表；
  写入 `result.json` 的会话级记录；无重启时不新增键。
- 验收/证据：`tests/test_executors.py` 与 `tests/test_solver_loop.py` 覆盖指标存在性与单会话向后兼容；
  只观测、不排序、不改写简报内容。
- 依赖：G0 已合入（重启路径存在）。
- 停止条件：若指标需要 schema 版本升级，先与 ZCP-09 的字段布局对齐再实现。

### ZCP-14 知识语料扩容

- 交付：`scripts/fetch-knowledge.sh` 增加第一批 pinned revision 语料（payload 手册、产品/中间件知识、
  方法论），更新 `knowledge/` 索引与许可说明，扩展 `tests/test_knowledge_catalogue.py` 的结构校验。
- 验收/证据：离线结构测试全绿；运行期无网络依赖；PR 记录来源 URL、revision 与许可证。
- 依赖：无。
- 停止条件：许可不明或需要登录的来源不收录，缺口登记为待办。

### ZCP-13 会话级收尾模板复用（条件项）

- 交付：把 ZCP-10 的四段收尾摘要模板复用到重启与取消路径；摘要仍是非权威文本，只用于接力与复盘。
- 验收/证据：`tests/test_solver_loop.py` 覆盖重启/取消路径的缺失段落可观测；
  `result.json` 不把摘要当证据引用。
- 依赖：D3 结束时 G0 与 P0 按期完成才启动。
- 停止条件：任何滑期或边界疑问都推迟到赛后窗口。

### G3 联调日（证据回收，不进新编号）

- 交付：执行 ZCP-01 实时核对清单；EXE-01 isolation probe 实跑（含 CI 步骤）；
  EXE-02 live Pi/Gateway smoke；BEN-02 target-only egress 与公开出口拒绝证据。
- 验收/证据：按 run-dir 与 PR 归档命令、结果与跳过原因；只有当日有证据的 roadmap 行才更新状态，
  否则保持 `[~]` 并写明缺口。
- 依赖：Docker、平台与 Pi 环境及凭据。
- 停止条件：环境不可得时整日产物是 blocked 记录，不得用本地 mock 替代实测。

## 4. D1-D5 排期

| 日 | 目标 | 出口判据 |
| --- | --- | --- |
| D1 | G0：提交 PR-1 与 PR-2；预约 D5 联调环境 | 两个 PR CI 绿；环境预约有明确窗口 |
| D2 | ZCP-01 离线部分 | 契约测试＋preflight＋核对清单完成，联调待执行 |
| D3 | ZCP-02＋ZCP-14 第一批；D3 末阶段二审批门 | 限流/实例对账测试绿；语料结构测试绿；复核包已提交并有答复 |
| D4 | ZCP-04＋ZCP-12；按期则 ZCP-13 | 阈值测试绿；简报指标测试绿；获批时启动阶段二最小实现 |
| D5 | G3 联调日＋证据归档＋阶段二收口 | 证据归档完成；未获证据项标 blocked；阶段二按审批门收口 |

排期保护：任何一天滑期，优先砍 ZCP-13 与 ZCP-14 增量，保住 G0 与 ZCP-01/02/04；
平台环境不可得时 D5 改为离线冲刺日，联调顺延并如实登记。

## 5. 验收与证据

- 离线验收以测试文件为准：`tests/test_tsec_adapter.py`（ZCP-01/02/04）、
  `tests/test_solver_loop.py` 与 `tests/test_executors.py`（ZCP-05 阶段一、ZCP-12/13）、
  `tests/test_knowledge_catalogue.py`（ZCP-14）。
- 常规门禁：CI 同款 namespace 扫描、`ruff check .`、`pytest -q`、
  `pytest tests/test_docs_consistency.py -q`；Docker/Pi 跳过的用例必须在联调日显式执行。
- 证据归档：联调结果按 run-dir 与 PR 记录；ZCP 状态更新只发生在带证据的 PR，
  且由 [agent-engineering.md](agent-engineering.md) 第 5 节承载。
- 反虚报：离线绿测不等于提分；未执行的实时核对一律 blocked，不写"已对齐"。

## 6. 明确不做（本窗口）

- C2（人类可读复盘时间线）与 C4（成本字段完整性）保持无编号候选，赛后窗口再评估。
- 不复活已 descoped 的 PLN-01..03/05..07、BEN-03/04、QA-02。
- 不把黑板/蚁群/角色编排/FSM/向量检索搬进 Harness 核心（见
  [agent-architecture-v2.md](agent-architecture-v2.md) 的非目标）。
- 不做无界并发、不绕过平台限流、不在未获批复的情况下改执行边界。
- 不在本窗口重做接入面实现；ZCP-01 先核对、再按差异另开 PR。

## 7. 风险与回退

| 风险 | 触发信号 | 回退 |
| --- | --- | --- |
| 平台不可达或凭据缺失 | D5 前无可用凭据 | ZCP-01 标 blocked，保留离线契约与核对清单；不影响其他项 |
| 阶段二被否或未答复 | D3 门无明确批准 | 不写代码；回退为增强会话级重启简报（阶段一＋ZCP-12） |
| 窗口滑期 | D3 结束时 G0/P0 未按期 | 砍 ZCP-13 与 ZCP-14 增量，先保住 ZCP-01/02/04 |
| 联调环境不可得 | Docker/Pi 不可用 | 记录 blocked 与环境需求；不用 mock 冒充实测 |
| 语料许可不明 | 来源无明确许可 | 不收录，登记缺口与替代来源 |

## 8. 参考

- [zc-philosophy-comparison.md](zc-philosophy-comparison.md)：两届 Top20 的架构/理念全景与 Harness 取舍，本文取自其第 6 节的 T1-T4 与 C1-C5 候选。
- [zc-improvement-design.md](zc-improvement-design.md)：A/B/C 段的实现设计与测试矩阵。
- [agent-engineering.md](agent-engineering.md)：ZCP 状态、赛场约束与 P0/P1 表格；本文新增 ZCP-12/13/14 的排期说明。
- [agent-architecture-v2.md](agent-architecture-v2.md)：Agent-Native Harness 的当前设计与非目标。
- [security/execution-boundary.md](security/execution-boundary.md)：执行边界的复核依据。
- [roadmap/README.md](roadmap/README.md)：子系统 roadmap 索引与全局验收门。
