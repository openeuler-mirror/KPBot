# Review、环境还原与案例归档

本文定义 Phase 3 的收尾流程。输出报告后不得直接结束，必须 review、还原并归档。

## Review

Review 目标：确认最终交付是否可信、可回退、可复用。

必须检查：

- 最终有效配置。
- 已采纳动作和收益。
- 被拒绝动作、失败动作和暂缓动作。
- 回退动作是否已执行或是否需要人工执行。
- 残留风险和下一步计划。
- 收益口径是否区分配置收益、运行点收益、诊断发现和 workload 变化。
- 是否仍存在未识别瓶颈。

输出字段：

- `review_result.status`
- `review_result.accepted_actions`
- `review_result.rejected_actions`
- `review_result.residual_risks`
- `review_result.next_steps`

## 环境还原

环境还原基于 Phase 1 生成的 `restore_baseline_manifest` 和每轮 rollback 记录。

还原策略：

| 场景 | 处理 |
|---|---|
| `analysis_only` | 无需还原，确认未执行变更 |
| `dry_run` | 无需还原，归档 dry-run 结果 |
| 在线变更已执行 | 执行 rollback 或输出人工命令 |
| 服务重启变更 | 还原配置并重启服务验证 |
| 系统重启/BIOS 变更 | 输出人工窗口期和检查清单 |
| 重编译/二进制替换 | 恢复上一轮二进制、配置、库和启动参数 |

输出字段：

- `restore_result.status`
- `restore_result.restored_items`
- `restore_result.pending_manual_items`
- `restore_result.validation_evidence`

## 案例归档

案例归档用于后续迭代和案例复用。归档必须脱敏。

`case_archive.json` 最低字段：

```json
{
  "schema_version": "1.0",
  "scenario_name": "",
  "workload_type": "",
  "deployment_topology": "",
  "target_resource_profile": "",
  "overall_progress": {},
  "workflow_gate_status": [],
  "current_run_id": "",
  "current_run_started_at": "",
  "current_run_manifest": {},
  "current_evidence_status": "",
  "current_evidence_paths": [],
  "service_health_status": "",
  "service_health_checks": {},
  "service_health_evidence": [],
  "historical_records_status": "",
  "historical_records_user_confirmation": "",
  "environment_backup_dir": "",
  "environment_diagnosis": {},
  "baseline_metrics": {},
  "bottleneck_classification": "",
  "performance_signal_summary_path": "",
  "candidate_skill_list": [],
  "dynamic_route_plan": [],
  "accepted_actions": [],
  "rejected_actions": [],
  "per_skill_iteration_state": {},
  "improvement_summary": {},
  "final_report_path": "",
  "review_result": {},
  "restore_result": {},
  "reuse_tags": [],
  "lessons_learned": [],
  "created_at": ""
}
```

归档规则：

- 保留技术证据路径，不保留敏感账号、密钥、客户业务数据。
- 使用 `<placeholder>` 替代 IP、用户名、客户路径和容器名。
- 归档总体进度、阻塞门控、服务健康检查结果和目标实例身份确认状态。
- 归档本轮 `current_run_id` 和证据新鲜度状态，保证案例复用时不会误当成新一轮现场结果。
- 归档环境诊断结果，包括历史 reference 问题集状态、BIOS 高性能配置状态、perf/PMU 采集能力状态和内核补丁检查状态。
- 归档历史记录状态；未经用户确认的历史记录必须标记为 `discovered_unconfirmed`，不得写成已验证优化收益。
- 标注可复用条件与不可复用条件。
- 标注导致 skill 停止的 5 轮 <1% 证据。

## 推荐脚本

可使用 `scripts/archive_case.py` 从报告输入、候选池、review 和 restore 结果生成归档骨架。
