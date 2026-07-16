# 候选 Skill 分析清单

本文件定义候选 skill 分析阶段的检查项。主框架根据本轮采集信息生成 `candidate_skill_list`，先执行证据命中的候选 skill，再执行 coverage 阶段中未命中的主优化 skill。

## 前置条件

- 基线已确认。
- 环境备份已完成。
- `bottleneck_classification` 已生成。
- `current_run_id`、`current_run_manifest` 已生成。
- `evidence_snapshot_dir` 已生成，且 `snapshot_metadata.json.current_run_id` 与本轮一致。
- `performance_signal_summary.json` 已生成，或已说明性能摘要降级原因。
- `current_evidence_status=current`，且 `snapshot_time` 不早于 `current_run_started_at`。
- `snapshot_metadata.json.target_identity` 与本轮目标实例身份一致。
- 若 run_id、证据状态、采集时间或目标实例身份任一项不匹配，不得生成任务包或候选动作。
- `agent_action_mode` 已确认。

## 执行模型

1. 读取 `candidate-skill-list.md`。
2. 根据 `performance_signal_summary.json` 生成 `candidate_skill_list`。
3. 使用 `scripts/create_subagent_tasks.py` 生成 `candidate-skill-tasks/` 任务包，并传入 `--current-run-id`、`--current-run-manifest`；只有人工指定候选时才传 `--subskills`。
4. 为每个候选 skill 启动独立分析 subagent；只生成任务包但不启动 subagent 不算完成。
5. 每个候选 skill 在独立 subagent 上下文读取自己的 `SKILL.md`。
6. subagent 只分析，不修改系统。
7. 主 agent 记录 `subagent_invocation_log` 并合并候选池。
8. 进入迭代优化阶段前做 gate check。

## 输出要求

每个候选 skill 必须输出：

- `subskill_name`
- `current_run_id`
- `current_evidence_status`
- `status`
- `analysis_timestamp`
- `evidence_sources`
- `findings`
- `candidate_actions`
- `required_evidence`
- `confidence`
- `fallback_notes`
- `timing`
- `subagent_id`
- `result_path`

候选动作必须包含实施、验证、回退、风险和不采纳条件。缺少这些字段的动作不得进入执行阶段。

coverage 阶段 skill 即使没有候选动作，也必须输出 `status=ok|degraded|blocked`、证据说明和“为何不产生动作”的结论，供最终报告记录。

## 完成标志

- `candidate_skill_list` 已落盘。
- 任务包、subagent 输出和 `candidate_pool.json` 的 `current_run_id` 一致。
- 所有 `candidate_skill_list` 中的 skill 均有 `ok`、`degraded` 或 `blocked` 输出。
- `subagent_invocation_log` 覆盖所有 `candidate_skill_list` 条目，且每条都有任务包、subagent ID、结果路径和状态。
- `candidate_pool.json` 已生成。
- `optimization_order` 已确定。
- 候选阶段和 coverage 阶段均已执行；若某 skill `blocked`，已列出补充证据或转入报告说明。
