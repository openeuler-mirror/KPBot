# 报告自动生成

优化结束时必须生成报告文件，不得只在对话中输出结论。

## 用法

当前脚本入口：

```bash
python3 scripts/generate_report.py \
  --input report-input.json \
  --output final-report.md
```

`report-input.json` 应按 `references/report-schema.md` 准备。若字段不足，先补齐输入，不要生成残缺报告。

## 报告结构

最终报告至少包含：

1. 总体进度标识：`overall_progress`、当前门控、阻塞门控、下一步。
2. Workflow 执行计划和阶段轨迹：`workflow_execution_plan`、`workflow_stage_trace`。
3. 本轮运行身份：`current_run_id`、`current_run_started_at`、`current_run_manifest`、`current_evidence_status`。
4. 场景、应用、workload、目标规格和 `scenario_environment_summary`。
5. 多节点与容器信息：`node_inventory`、`container_targets`、`container_execution_mode`。
6. 环境信息和软硬件信息。
7. 环境诊断：历史 reference 问题集、BIOS 高性能配置、perf/PMU 可用性、内核补丁齐全性、`per_node_environment_diagnosis`，以及 `environment_diagnosis_confirmation_status`。
8. 服务健康检查和目标实例身份。
9. 测试组网和测试用例信心。
10. 基线数据和用户确认状态。
11. 调优 workflow 流程分析。
12. 瓶颈识别和证据。
13. 性能采集摘要和候选优化 skill 列表。
14. 各优化手段验证效果和耗时：`agent_timing_summary`、`per_skill_timing_summary`、`optimization_timing`、`optimization_timing_details`。
15. 单 skill 停止原因和全局停止原因。
16. review、环境还原和案例归档。
17. 结论和下一步计划。

## 阻塞报告

若 `environment_diagnosis_confirmation_status != confirmed`、`service_health_status != passed`、`target_instance_identity.status != confirmed`、`baseline_confirmation_status != confirmed`、`current_run_id` 缺失或 `current_evidence_status != current`：

- 报告摘要必须写明 `blocked` / `degraded` 和阻塞门控。
- 若环境诊断未确认，必须输出环境诊断结果、阻塞项/降级项、证据路径、用户确认状态和下一步确认/补采/修复建议。
- 必须输出服务健康检查结果、失败原因、证据路径和修复建议。
- 必须输出本轮运行身份、证据状态、证据新鲜度失败原因和补采建议。
- `bottleneck_classification` 必须为 `not_entered` 或 `blocked`，不得输出正式瓶颈分类。
- `candidate_skill_list`、`candidate_pool`、`per_skill_iteration_state` 必须为空或 `not_entered`，不得伪造优化轮次。
- 历史日志和旧报告只能放在“待用户确认的历史记录”章节，不能作为最终优化结论。
- 不得生成正式收益表；若报告中保留表结构，必须明确写为 `not_entered` 或 `blocked`。

## 完成态报告硬校验

当 `overall_progress.status=completed` 且不是阻塞报告时，报告输入必须包含非空：

- `workflow_execution_plan`
- `workflow_stage_trace`
- `agent_timing_summary`
- `per_skill_timing_summary` 或可由 `timing_jsonl_path` 自动汇总出的等价按 skill 耗时表
- `optimization_timing`
- `optimization_timing_details`

缺失任一字段时，报告生成应失败或停在 `report_input_validation`，提示补齐 workflow 阶段或耗时记录。不得生成“完成态但没有流程和耗时”的最终报告。

若报告输入包含 `timing_jsonl_path`，`scripts/generate_report.py` 必须在 `optimization_timing`、`optimization_timing_details` 或 `agent_timing_summary` 缺失时自动读取 JSONL，并生成按 skill 汇总耗时；自动汇总失败时必须在报告中输出 `timing_load_warnings`。

## 收益表

收益表必须区分：

- 阶段收益。
- 累计收益。
- 诊断发现。
- workload 或测试方法变化。
- 历史记录状态：未确认的历史记录不得进入收益表。

不得把 query mix、硬件规格变化或测试方法变化包装成服务器配置收益。
