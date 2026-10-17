# 智能体架构（superseded）

本文件是 v1 设计：Harness 参与认知——把技能元数据投影成"意图-事实前沿"，用确定性规划器
挑选候选，再用 Manager/Solver/Observer 角色编排分派工作。**该设计已被 v2 取代。**

当前设计：[agent-architecture-v2.md](agent-architecture-v2.md) —— Agent-Native Harness：
Harness 供给环境与信任，模型拥有认知；认知层是文件系统里的笔记与只读 markdown 知识，
不再有计划器、前沿投影或角色编排。

v1 内容可按提交 `8021b2f` 及更早版本查阅；前沿算法本身冻结为离线消融基线，
见 `tests/ablation/` 与 `tests/test_ablation_frontier.py`。
