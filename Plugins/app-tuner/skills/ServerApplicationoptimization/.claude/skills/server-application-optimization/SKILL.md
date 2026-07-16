---
name: server-application-optimization
description: Claude Code Dynamic Workflows 入口。按门控加载主源 skill，根据性能采集信息动态生成候选优化 skill 列表并调度 workflow，而不是线性执行所有优化分支。主源位于 skills/server-application-optimization/SKILL.md。
---

# Server Application Optimization Agent (Claude Code Dynamic Workflows Entry)

本文件是 Claude Code 的平台入口。它只定义 Claude Code 如何启动 Dynamic Workflows；实现内容仍以主源为准。

**请加载主源**: `skills/server-application-optimization/SKILL.md`

主源目录包含完整 SKILL.md、references/、subskills/、scripts/ 等全部资源。本入口仅用于 Claude Code 平台的 skill 发现，所有逻辑、流程和约束定义均在主源中维护。

## Claude Code Dynamic Workflow Contract

Claude Code 调用该 skill 时必须按以下运行时契约执行：

1. 先加载主源 `skills/server-application-optimization/SKILL.md`，再按主源的 Phase 1 强制读取列表加载 references。
2. 初始化 `current_workflow_state`，至少包含 `current_gate`、`completed_gates`、`blocked_gate`、`next_gate`、`current_run_id`、`evidence_status`、`candidate_skill_list`、`active_workflow` 和 `workflow_trace`。
3. 启动阶段只进入 `bootstrap`、`scenario-intake`、`environment-baseline` 等上游 workflow；不得提前批量读取或执行所有 subskills。
4. 基线确认和深度证据采集完成后，读取 `references/candidate-skill-list.md` 并生成 `candidate_skill_list`。
5. 优先加载候选列表命中的 subskill；未命中的主优化 subskill 只在 coverage 阶段加载并形成结论。
6. 每个被路由 workflow 只输出候选动作、风险、验证方法和回退方法；真实变更必须再次确认 `agent_action_mode=approved_execute`。
7. 单个 workflow 停止后，如果仍有候选或 coverage 项，继续下一个 workflow；不得退回静态全量流程。
8. 报告必须包含 `workflow_trace`，说明每次门控、路由、跳过、阻塞、回流和停止原因。
