## 最终输出格式

只展示假设、实验次数、关键证据、转向原因和最终状态；原始实验日志保留在 run_dir 的文件中。

| 优化点 | 假设 | 实验次数 | 关键证据 | 转向原因 | 最终状态 |
|--------|------|----------|----------|----------|----------|
${diagnostic_summary_rows}

由各点结果生成 diagnostic_summary_rows：hypothesis_id/claim、已消费 experiment_count、实际 record_path 与 evidence_refs、transition_reasons、diagnostic_status 和三项 verdict（correctness_verdict / local_performance_verdict / system_acceptance）。失败候选给出 candidate_path/candidate_patch 与 parent_state，等待目标列出缺项。局部结果明确 scope；累计收益只用完整系统同条件前后实测，缺失则未完成。verified 只代表完整正确性与系统验收均通过，不能把 unverified/marginal 或局部提速列入已验收成功。

本期保存实验记录，方法库后续建设。

### Batch 机器可读结果

成功、等待、预算耗尽、阻塞或无优化空间均写 `${context.run_dir}/batch_result.json`，保留旧字段并追加诊断摘要：

```json
{
  "pipeline_status": "completed|blocked|failed",
  "quality_status": "applied_verified|applied_unverified|complete_no_optimization|baseline_blocked|pipeline_incomplete|artifact_error|report_inconsistent|driver_failed",
  "applied_count": 0,
  "verified_count": 0,
  "clean_patch_files": [],
  "blocked_reason": null,
  "performance_summary": null,
  "diagnostic_summary": [{
    "optimization_point_id": "<point_id>",
    "hypothesis_id": "<hypothesis_id>",
    "experiment_count": 0,
    "record_paths": [],
    "evidence_refs": [],
    "transition_reasons": [],
    "final_status": "awaiting_target_evidence|stop_budget|blocked_*|unresolved|skipped|verified",
    "correctness_verdict": "not_run",
    "local_performance_verdict": "not_run",
    "system_acceptance": "not_run",
    "candidate_path": null,
    "candidate_patch": null,
    "parent_state": "<accepted source identity>"
  }]
}
```

applied_count 统计实际应用候选（含未验收），verified_count 只统计 CLI Deliver 后完整验收的点；clean_patch_files 只列已验收可交付 patch，失败/未验收候选在 diagnostic_summary 中单独留存。等待目标、stop_budget、blocked_*、unresolved 有剩余验收工作时 pipeline_status=blocked、quality_status=pipeline_incomplete 并给 blocked_reason；预算耗尽不是完成优化或成功。已应用未验收候选不能显示 applied_verified。complete_no_optimization 仅用于明确无优化空间/跳过且 applied_count=0、无源码/config patch，不可掩盖证据缺失。PrepareProject/baseline 阻塞用 baseline_blocked 并保留 baseline_blocked.json。达到最大轮次同样如实保留最终状态，不能把未完成验收转成成功。

### 路由摘要

- DesignExperiment 缺少判据或身份 → blocked_evidence。
- ValidateExperiment → 使用完整 CLI next_stage；反证回 AnalyzeHotspot，未决回 DesignExperiment，有支持才 Decide。
- Verify/ReVerify 编译或功能失败 → FixCode；性能失败或系统未验收 → ValidateExperiment。
- FixCode 耗尽 → unresolved，保留失败候选；下一候选前无法确认父状态安全则 blocked_parent_state。
- 所有 Deliver 必须经过候选 CLI 一致性校验；失败候选不自动 stash/清理。
