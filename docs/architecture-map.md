# 架构图：Agent-Native Harness

> 本文把 [agent-architecture-v2.md](agent-architecture-v2.md) 的一页设计落成可核对的结构图。
> 一句话基线：**Harness 供给环境与信任，模型拥有认知。**
> 图中的每个边界都有强制点：`tests/test_architecture.py`（import 方向与冻结的 control spine）、
> `tests/test_executors.py`（会话隔离）、[security/execution-boundary.md](security/execution-boundary.md)
> （容器挂载与信任分层）。
>
> 图源就是下面的 mermaid 代码块（GitHub 直接渲染）。同样五幅图也预渲染成了离线文件，
> 放在 `docs/diagrams/` 下，每个名字都有 `.png` 与 `.svg` 两份，顺序与下文一一对应：
> `01-components`、`02-run-loop`、`03-modules`、`04-trust-boundary`、`05-competition`。
> 改动代码块后重新渲染对应的那一幅即可：
> `npx -y @mermaid-js/mermaid-cli@11.4.2 -i <block>.mmd -o docs/diagrams/<name>.svg -b white`。

## 1. 组件与数据流

```mermaid
flowchart TB
    subgraph ops["操作者 / CI"]
        CLI["harness CLI<br/>run · tsec · report · audit-run · hint · preflight"]
    end

    subgraph host["Harness 进程（可信）"]
        direction LR
        ORCH["orchestrator.py<br/>本地任务生命周期"]
        BRN["benchmark/runner.py<br/>外部题目生命周期"]
        BOOT["runtime/bootstrap.py<br/>唯一组合点"]
        LOOP["solver_loop.py<br/>单闭环控制"]
        POLICY["solver_policy.py<br/>纯止损律"]
        WORLD["world/<br/>事件溯源状态"]
        ACCT["progress · budget · trace<br/>记账与观测"]
        VER["verification · verifier<br/>权威裁决"]
        GW["gateway.py<br/>工具授权 + 模型代理"]
        ORCH --> BOOT
        BRN --> BOOT
        BOOT --> WORLD
        BOOT --> ACCT
        BOOT --> LOOP
        LOOP --> POLICY
        LOOP --> WORLD
        LOOP --> VER
        VER --> WORLD
    end

    subgraph untrusted["Agent 侧（不可信执行）"]
        AG["Pi RPC / CLI / Docker Agent<br/>/task:ro · /knowledge:ro · /run/harness:rw"]
    end

    subgraph ext["外部系统"]
        MODEL["模型 Provider"]
        PLAT["TSec 平台<br/>SDK openapi/v1"]
        TARGET["靶机环境"]
    end

    CLI --> ORCH
    CLI --> BRN
    LOOP -->|"prompt · steer"| AG
    AG -->|"agent.events.jsonl<br/>未校验观测"| LOOP
    AG -->|"模型与工具调用<br/>一次性 Gateway 令牌"| GW
    GW --> MODEL
    BRN -->|"provision · submit · evaluate"| PLAT
    PLAT --> TARGET
```

要点：

- `runtime.bootstrap_solver` 是本地运行与外部 benchmark 的唯一组合点，两条入口线因此共享
  同一套 World / Progress / Trace / Verifier 装配。
- Agent 只有一条回传通道：写在可写工作区里的 JSONL 观测。它们先过 `TypedACI` 校验，
  再由 Harness 以 `actor=agent` 写入世界，Agent 永远不直接写 `world.db`。
- Gateway 是凭据边界：模型 provider key 留在 Gateway 进程，容器只拿到一次性
  `HARNESS_GATEWAY_TOKEN`。

## 2. 一次运行的闭环时序

```mermaid
sequenceDiagram
    autonumber
    participant OP as 操作者 / CI
    participant EN as Orchestrator / BenchmarkRunner
    participant BOOT as runtime.bootstrap_solver
    participant SL as SolverLoop
    participant AG as Agent 会话
    participant W as World（world.db）
    participant VE as Verifier / Benchmark 评测

    OP->>EN: harness run / harness tsec
    EN->>EN: 校验 task 与 agent 契约、算哈希、建 run_dir
    EN->>BOOT: 组合 solver 运行时
    BOOT->>W: seed 根 Goal 与目标实体
    BOOT-->>SL: World · Progress · Trace · Verifier registry
    EN->>AG: 启动会话（/task 与 /knowledge 只读，/run/harness 可写）
    SL->>AG: prompt 目标 + 事实投影 world.context.txt
    loop 每个 Agent 事件
        AG-->>SL: agent.events.jsonl（不可信输入）
        SL->>W: TypedACI 校验通过后 ingest
        SL->>VE: 验证事件或提交候选
        VE-->>SL: 裁决（证据 / 提交结果）
        SL->>AG: steer 验证反馈、hint、预算与重复动作告警
        SL->>SL: solver_policy 止损律判定
    end
    SL->>W: 收尾投影（Goal 结果、run summary）
    EN->>VE: 客观判定或 benchmark 评测
    EN->>AG: 关闭会话
    EN->>OP: result.json · trace.jsonl · audit-run
    Note over EN, W: checkpoint.json 记录控制游标与已用量，崩溃后据此对账恢复
```

止损律是纯函数（`solver_policy.decide_solver_action`）：无进展上限、未确认进展上限、
预算耗尽、低预算且无进展。它只决定 continue / verify / stop，不做规划、不打分、不排序。

## 3. 模块分层与依赖方向

```mermaid
flowchart TB
    CLI["cli.py<br/>入口：run · tsec · report · audit-run · hint"]
    GW["gateway.py · gateway_runtime.py<br/>入口：工具授权与模型代理"]
    APP["orchestrator.py · benchmark/runner.py<br/>应用生命周期"]
    BEN["benchmark/（base · protocol · tsec_*）<br/>基准适配层"]
    BOOT["runtime/bootstrap.py<br/>唯一组合点"]
    LOOP["solver_loop.py · solver_policy.py · aci.py · action_verifier.py<br/>闭环层"]
    SESS["session.py · agent.py · pi_adapter.py · pi_container.py"]
    VER["verification.py · verifier.py"]
    ACCT["budget.py · progress.py · trace.py · extract.py · hints.py"]
    RTI["runtime/（checkpoint · executors · lifecycle · projections · results · evidence）"]
    WORLD["world/<br/>事件溯源世界状态"]

    CLI --> APP
    CLI --> GW
    APP --> BEN
    APP --> BOOT
    BOOT --> LOOP
    BOOT --> RTI
    LOOP --> SESS
    LOOP --> VER
    LOOP --> ACCT
    LOOP --> WORLD
    VER --> WORLD
    RTI --> WORLD
```

图中只画允许的方向（实线）。反向依赖（`world/ → benchmark|runtime|入口`、
`runtime/ → benchmark|入口`、`benchmark/ → 入口`）连同 `fastapi`/`docker`/`httpx`/`tsec_benchmark`
这类基础设施导入一律禁止：

强制规则（[tests/test_architecture.py](../tests/test_architecture.py)）：

| 规则 | 内容 |
|---|---|
| 方向 | `world/` 不得导入 `benchmark/`、`runtime/`、`orchestrator`、`cli`、`gateway*`；`runtime/` 不得导入 `benchmark/` 与入口模块；`benchmark/` 不得导入入口模块 |
| 基础设施 | `world/` 与 `runtime/` 不得导入 `fastapi`、`starlette`、`docker`、`subprocess`、`httpx`、`tsec_benchmark`；`benchmark/` 不得导入 `fastapi`、`starlette`、`docker` |
| 冻结闭环 | 从 `runtime.bootstrap`、`benchmark.runner`、`solver_loop` 可达的模块必须显式列入 `CONTROL_SPINE`，新模块进闭环要被 reviewer 明确接受 |
| 删除项 | `harness.api`、`control_plane`、`queue`、`worker`、`coordination`、`otel`、`scoring`、`suite`、`evaluation`、`registry`、`cognition`、`planner`、`skills`、`crew`、`runtime.solver_profile` 不得以任何形式复活 |

## 4. 运行目录与信任边界

```mermaid
flowchart TB
    subgraph host["宿主侧（可信）"]
        direction LR
        subgraph spec["运行输入"]
            TASK["task.yaml + 任务目录"]
            KNOW["skills/offensive · knowledge/<br/>只读 markdown 知识"]
        end
        subgraph runroot["run_dir：Harness 独占，任何会话容器都挂不到"]
            direction TB
            T1["权威：world.db · world.events.jsonl<br/>world.snapshot.json · trace.jsonl<br/>progress.json · checkpoint.json<br/>result.json · evidence/ · verifier.*.log"]
            H1["不可信：agent-workspace/<br/>events.jsonl · world.inbox.jsonl<br/>submission.inbox.jsonl · notes/ · agent.summary.md"]
            SH["executor-shared/<br/>shared-findings.md"]
            T4["hints.jsonl（仅宿主可写）"]
        end
    end

    subgraph box["Agent 容器（不可信执行）"]
        C1["/task:ro<br/>任务快照"]
        C2["/knowledge/&lt;name&gt;:ro"]
        C3["/run/harness:rw<br/>工作区 + 投影副本"]
        C4["/run/harness-shared:rw"]
    end

    TASK -->|"剔除 verifier · 拒软链 · ≤4096 文件/2GiB"| C1
    KNOW -->|"Harness 不解析、不排序"| C2
    H1 -->|"rw 挂载；Harness 回读时按敌意输入处理"| C3
    T1 -.->|"只读副本 world.context.txt / progress.json"| C3
    T4 -->|"SolverLoop 记为 human.hint 并 steer"| C3
    SH -->|"唯一跨会话可写面"| C4
```

信任分层（`world.models.TRUST_RANK`）：`claim` < `evidence` < `verified`。
Agent 自述与工具退出码只到 claim；只有授权 verifier 或 benchmark evaluator 能签发
evidence / verified，裁决必须引用 Harness 持久化的证据。

容器可达面之外的东西都是不可协商的：run root、world 存储、result、checkpoint、证据、
verifier 日志与凭据都不进容器。多会话竞争中唯一新增的跨会话可写面是 `executor-shared/`，
只允许放 Agent 自写的 `shared-findings.md`。

## 5. 同题多会话竞争

```mermaid
flowchart TB
    CLI["harness tsec --parallel N（N ≤ 3 道题）"] --> CH

    subgraph CH["一道题：一次 provision，一个靶机实例"]
        direction TB
        P["BenchmarkRunner.run_case<br/>plan_executor_sessions"]
        P --> S1["executor 01<br/>run_dir/executors/01-*/"]
        P --> S2["executor 02<br/>run_dir/executors/02-*/"]
        P --> S3["executor 03<br/>run_dir/executors/03-*/（上限 4）"]
        SHARED["executor-shared/<br/>shared-findings.md"]
        S1 <--> SHARED
        S2 <--> SHARED
        S3 <--> SHARED
    end

    CH --> RES["共享面：一个 world · 一条权威 trace.jsonl<br/>一个提交去重集合 · 一次最终裁决 · 一份 result.json"]
    RES --> CONV["任一会话提交被接受且评测成功<br/>→ 关闭其余会话并释放实例"]
```

预算按会话均分（token / 模型调用 / 工具调用 / 成本向下取整，下限 1），
wall_time 不切分，因为并发会话共享同一个赛窗。单个会话触达止损律只关自己；
全部会话结束或预算耗尽才结束该题。

## 6. 图与代码的对应关系

| 图 | 组件 | 代码 | 强制点 |
|---|---|---|---|
| 1 | 唯一组合点 | `src/harness/runtime/bootstrap.py` | `CONTROL_SPINE` 冻结闭包 |
| 1、2 | 单闭环控制 | `src/harness/solver_loop.py` | `tests/test_solver_loop.py` |
| 1、2 | 纯止损律 | `src/harness/solver_policy.py` | `tests/test_control_law_properties.py` |
| 1、2 | 事件校验入口 | `src/harness/aci.py`、`agent_events.py` | `tests/test_solver_loop.py` |
| 1、2 | 预算记账 | `src/harness/budget.py` | `tests/test_budget.py`、`tests/test_control_metrics.py` |
| 1 | 世界状态 | `src/harness/world/` | `tests/test_sqlite_world.py`、`tests/test_world_context.py` |
| 1 | 凭据隔离 | `src/harness/gateway.py`、`gateway_runtime.py` | `tests/test_gateway.py`、[security/gateway-tools.md](security/gateway-tools.md) |
| 2 | 崩溃对账 | `src/harness/runtime/checkpoint.py` | `tests/test_checkpoint.py` |
| 2 | 权威裁决 | `src/harness/verification.py`、`verifier.py` | `tests/test_verification_evidence.py`、`tests/test_verifier.py` |
| 1 | 提示通道 | `src/harness/hints.py` | `tests/test_hint_channel.py` |
| 3 | 依赖方向 | 全包 | `tests/test_architecture.py` |
| 4 | 容器挂载 | `src/harness/agent.py`、`pi_container.py` | [security/execution-boundary.md](security/execution-boundary.md) |
| 5 | 会话隔离与预算切分 | `src/harness/runtime/executors.py`、`benchmark/runner.py` | `tests/test_executors.py` |
