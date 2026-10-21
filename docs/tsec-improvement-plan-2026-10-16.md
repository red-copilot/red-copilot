# TSec 运行数据改进计划（2026-10-16）

> 依据：[tsec-run-review-2026-10-16.md](tsec-run-review-2026-10-16.md) 之后的第二轮全量数据分析——
> `.harness/tsec-runs/` 99 个目录（36 份 `result.json`、56 条 trace、18,681 个事件）、41 份
> `agent.feedback.jsonl`（3,154 条回灌）、41 份 Pi 会话转录、8,787 行 sweep 日志。
> 定位：把"慢"与"不闭环"两个结构问题拆成可验收条目；认知仍归 Agent，Harness 只动环境、
> 信任、记账与恢复。效果验收只有一条：比赛得分；本计划的中间验收全部是离线可复现的确定性证据。

## 1. 数据结论（条目的事实基础）

| 结论 | 证据 |
| --- | --- |
| 失败全部是"零提交" | 15/15 失败 run `submissions=[]`；21/21 成功 run 各提交一次且一次命中（全量 0 次被拒） |
| 唯一真信号在环外 | 唯一带信息的信号是平台裁决（21 条 `benchmark.submission.result`），且出现在 run 末尾（a-04 1380/1382s、a-11 1670/1680s） |
| 环内反馈是噪音 | 3,154 条回灌中 72% 是零信息 `world.state.updated`、21% 是 `solver.verification`，而 656/656 条裁决全是 `pending`（理由 `world_revision_changed` 589、`expected_observation_missing` 576） |
| 控制律几乎不介入 | 5,358 次 `solver.decision` 中 4,213 次 `continue/no_control_action_required`、1,136 次 `verify`、仅 9 次 `stop` |
| 类型化 ACI 通道基本没被用 | 37 个 run 合计 122 条结构化 Agent 事件 vs 2,222 次工具调用 |
| 单轮成本无界 | 结束上下文 3.1k→20.6k（成功）/44.3k（失败，最大 73.6k）；19 次工具调用 >180s，最长 927s（占赛窗 51%）；ffuf 平均 94s/次；失败 run 占 69% 墙钟、71% token |
| 上下文是原始流水 | `world.context.txt` 恒定 ~18KB，主体是逐条 `tool.execution`/`action:*` 的 JSON（含完整 provenance），无预算或剩余时间；91 个工具结果读到它 |
| 笔记记忆未生长 | notes 中位 1 个文件 344 字节；a-05 全程 282 字节、a-16 结束时仍写 `Findings: (none yet)` |
| 装了不等于能用 | nmap `Operation not permitted` 28 次（a-17/b-01 侦察报废）；缺 unzip/xxd/php-cli/iptables、缺 python3 `requests`/`PIL`/`cv2`/`pytesseract`/`numpy`/`paramiko`/`flask`；sqlmap/nikto/dirb/gobuster/hydra/masscan 零使用 |

## 2. 条目

### BEN-06（P1，阶段 A）——把度量补齐

- **改法**：`result.json.metrics` 与 `sweep-summary-<UTC>.json` 统一补 `submissions_attempted`、
  `first_submission_ms`、`submitted_at_fraction`、`steer_counts{hint,finalize,submission,verification}`、
  `tool_time_ms`、`slow_tool_calls`（有界 top-N）、`context_tokens_final`；`harness report` 展示这些字段。
- **验收**：以检入的 trace 片段夹具离线复算，字段与历史 run 一致；无新模块。

### RUN-06（P0，阶段 B）——运行内节拍

- **改法**：把剩余预算与墙钟写进 Agent 可见的 `world.context.txt` 头部（只读、确定性），
  并保证 `progress.json` 的预算视图可查；benchmark 路径继续默认开启 RUN-05 的提交收尾
  （`finalize_requests_submission`、`submission_due_fraction`），本地任务路径保持关闭。
  世界 revision 与 pending verdict 只进 trace，不产生 steer。
- **验收**：离线单测覆盖预算段渲染与旗标默认值；历史 trace 回放不改变既有成功提交路径。

### RUN-07（P1，阶段 B）——死边降噪

- **改法**：`world.context.txt` 改为"事实摘要"——`tool.execution` 与 `action:*` 原始流水折叠成
  有界统计（调用数、失败数、最慢几条），语义事实（finding/observation/hypothesis/失败路径）
  保留；Agent 自写变更不回灌、`pending` 裁决不进 steer（benchmark 默认）。
- **验收**：回放 a-05 等历史 trace，单 run steer 从百级降到个位数；投影仍在 WLD-05 的 token 预算内。

### VER-05（P1，阶段 B）——独立观测契约

- **改法**：`verification.py` 增加 Harness 侧 `ObservationRecord`（producer/时间/world revision/artifact hash），
  观测源限定为平台裁决与 Harness 侧探测；Agent 的 `expected_observation` 不再充当比对基准——
  没有观测记 `unavailable` 且不 steer，有观测才比对。
- **验收**：单测覆盖"无观测→unavailable 不 steer""有观测→比对正确""伪造 producer 被拒"；
  VER-01/02 的权威模型与信任分层不变。

### EXE-07（P1，阶段 C）——工具可用性优先于工具数量

- **改法**：定位 nmap 的 `Operation not permitted`（Kali 包装脚本 exec 真二进制 + cap-drop/no-new-privileges）；
  修不动就移除并在 prompt 写清 masscan/nc 替代路径；补 `unzip`、`xxd`、`php-cli`、`iptables`、`traceroute`
  与离线预装的 `python3-requests`/`python3-bs4`/`python3-lxml`/`python3-pil`/`python3-pycryptodome`/`python3-paramiko`；
  更新 `tests/test_pi_kali_image.py` 静态契约（含"求解期不得安装依赖"）并记录镜像体积增量。
- **验收**：容器内替代侦察命令与 `python3 -c "import requests"` 通过；静态契约与 CI 镜像探针通过。
- **边界**：任何 cap/seccomp 变更先出结论再改代码，走 maintainer 复核。

### EXE-08（P2，阶段 C，条件项）——渲染观测（浏览器）

- **阶段一（"眼"）**：Harness 侧只读 `render(url)` → 状态码/DOM 文本/跳转链/XHR 列表，结构化落 trace，
  并作为 VER-05 的观测源；chromium + playwright 烘进镜像。
- **阶段二（"手"）**：是否给 Agent 自由浏览器由 maintainer 按执行边界复核决定
  （`cap-drop ALL` + no-new-privileges 下需要 `--no-sandbox`，属隔离降级）。
- **诚实标注**：a 系列 7 个失败 run 的卡点看起来在"找可绕过的判据"而非"渲染"，本条不承诺消掉它们。

### PLN-08（P2，阶段 C）——材料层纪律（只改 markdown）

- **改法**：`prompts/tsec-solver.md` 补"工具优先 + 单次调用预算 + 每 N 步写回 notes"；
  `skills/offensive/*` 补 ffuf 速率/递归与替代路径；`knowledge/methodology/*` 补"何时放弃路径"。
- **验收**：`tests/test_knowledge_catalogue.py` 与 docs 一致性测试通过；慢调用数与 notes 字节数进入下一轮观测。

### BEN-07（P1，阶段 D）——真实复跑验收

- 平台恢复后重跑 `b-03`、`c-02`..`c-06`、`a-03`、`a-05`、`e2-04`、`e3-04` 与 4 个已解对照组，
  用 BEN-06 的字段对比提交率 / 首提交时间 / 墙钟 / steer 计数 / token；任一条目指标恶化即回滚该条目
  （重点盯 RUN-07 降噪与 VER-05 的 `unavailable` 语义）。

## 3. 排期与依赖

BEN-06 → RUN-06 → RUN-07 → VER-05 →（EXE-07、PLN-08 可并行）→ EXE-08 阶段一 → BEN-07。
每个条目一个 PR；落地在既有模块，`CONTROL_SPINE` 与模块/LOC 上限保持不变（必要时重新测量而非提高）。

## 4. 不做的事

不复活 Harness 侧认知（规划、技能排序、候选注入）；不加零使用的 Kali 工具；不把浏览器当默认放开；
不动信任分层与裁决权威；不在离线环境假设运行时安装依赖；不为单一 benchmark 在核心层开特例。

## 5. 平台侧前置（沿用上一轮 operator 手册）

关闭平台上挂着的 challenge 实例、确认隧道与 DNS、用单次 sweep 替代 bash 重试循环，
按退出码处理（3=平台不可用、4=部分题无产出）后再做 BEN-07。
