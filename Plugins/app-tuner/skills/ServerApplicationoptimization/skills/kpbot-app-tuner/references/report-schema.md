# 报告字段约定 / Report Schema

最终报告必须服务于架构图中的输出报告模块，并可被案例归档复用。

## 必填字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scenario_name` | string | 场景名称 |
| `application_name` | string | 应用名称 |
| `workload_type` | string | 工作负载类型 |
| `deployment_topology` | string/object | 测试组网 |
| `test_topology_confidence` | string | 测试组网信心：`high` / `medium` / `low` |
| `test_case_confidence` | string | 测试用例信心：`high` / `medium` / `low` |
| `environment_snapshot` | object | 环境信息与软硬件信息 |
| `environment_backup_dir` | string | 环境备份目录 |
| `environment_diagnosis` | object | 环境备份后的诊断结果，包含历史 reference 问题集、BIOS 高性能配置、perf/PMU 可用性、内核补丁齐全性 |
| `environment_diagnosis_confirmation_status` | string | 环境诊断结果用户确认状态：`pending` / `confirmed` / `rejected` / `rebuild_required` / `blocked` / `not_entered` |
| `baseline_metrics` | object | 基线指标 |
| `baseline_confirmation_status` | string | 基线确认状态 |
| `target_instance_identity` | object | 目标实例身份校验证据 |
| `bottleneck_classification` | string | 最终瓶颈类型 |
| `bottleneck_evidence` | object | 瓶颈证据 |
| `workflow_trace` | array | 调优 workflow 流程分析 |
| `workflow_execution_plan` | array | 本轮计划执行的阶段、门控、确认点和预期产物 |
| `workflow_stage_trace` | array | 本轮实际执行阶段轨迹，包含开始/结束时间、耗时、状态和证据路径 |
| `performance_signal_summary` | object | 性能采集摘要，包含热点函数、热点 so、topdown 和线程切换信号 |
| `candidate_skill_list` | array | 根据采集信息生成的候选优化 skill 列表，包含证据候选和 coverage skill |
| `candidate_pool` | object/array | 候选动作池 |
| `optimization_actions` | array | 实际验证的优化动作 |
| `before_after_metrics` | array | 分阶段前后指标 |
| `improvement_summary` | object/array | 提升比例和收益口径 |
| `optimization_timing` | array | Agent 调优耗时统计 |
| `optimization_timing_details` | array | 各手段耗时明细 |
| `per_skill_timing_summary` | array | 按 skill 汇总的分析、实施、验证和总耗时 |
| `per_skill_gain_summary` | array | 按 skill 汇总的独立收益归因，字段定义见 `references/iteration-execution.md` |
| `skill_execution_order` | object | 实际执行顺序验证，包含 `execution_order[]`、`cpu_affinity_first_verified` 和 `cpu_affinity_completed_before_next` |
| `agent_timing_summary` | object | 全局耗时汇总，包含总耗时、各 phase 耗时、分析/实施/验证耗时 |
| `per_skill_iteration_state` | object | 每个 skill 轮次状态和停止原因 |
| `selected_optimization_actions` | array | 已采纳动作 |
| `rejected_optimization_actions` | array | 被拒绝或暂缓动作 |
| `review_result` | object | review 结论 |
| `restore_result` | object | 环境还原结果 |
| `next_steps` | array | 下一步计划 |
| `case_archive_path` | string | 案例归档路径 |
| `overall_progress` | object | 总体进度、当前门控、阻塞门控和下一步 |
| `workflow_gate_status` | array | 各阶段门控状态，至少包含服务健康、实例身份、基线确认、瓶颈识别、候选 skill 列表、迭代状态 |
| `current_run_id` | string | 本轮唯一运行 ID |
| `current_run_started_at` | string | 本轮启动时间 |
| `current_run_manifest` | object/string | 本轮输出目录、基线、证据、任务包、候选池和报告路径索引 |
| `current_evidence_status` | string | `current` / `missing` / `stale` / `mixed` / `invalid` |
| `service_health_status` | string | 目标服务健康检查状态：`pending` / `passed` / `failed` / `blocked` / `degraded` |
| `service_health_checks` | object/array | 端口、协议、认证、权限和最小负载 smoke test 的检查结果 |
| `service_health_evidence` | string/array | 服务健康检查原始日志路径 |
| `historical_records_status` | string | 历史记录状态：`none_found` / `discovered_unconfirmed` / `user_confirmed_usable` / `user_rejected` |
| `historical_records_user_confirmation` | string/object | 历史记录是否已由用户确认可用于本轮 |
| `scenario_environment_summary` | object | 用户输入摘要及环境概括 |
| `scenario_confirmation_status` | string | 用户是否确认场景摘要 |
| `node_inventory` | array | 多节点清单 |
| `per_node_environment_backups` | array | 多节点环境备份结果 |
| `per_node_environment_diagnosis` | array | 多节点环境诊断结果 |
| `container_targets` | array | 容器目标和进入方式 |
| `container_execution_mode` | string | 容器优先/宿主机降级等执行模式 |

## 建议字段

- `agent_action_mode`
- `change_scope`
- `dependency_status`
- `missing_dependencies`
- `degraded_capabilities`
- `flamegraph_path`
- `perf_data_path`
- `hot_functions`
- `hotspot_function_rank`
- `hotspot_dso_rank`
- `process_thread_summary`
- `topdown_summary`
- `performance_signal_summary_path`
- `hardware_capacity_recommendation`
- `online_vs_restart_changes`
- `stackability_notes`
- `risk_and_rollback`
- `raw_evidence_paths`
- `global_stop_reason`
- `service_health_failure_reason`
- `service_health_next_steps`
- `current_evidence_paths`
- `evidence_freshness_policy`
- `evidence_freshness_failure_reason`
- `evidence_freshness_next_steps`
- `historical_records_paths`
- `historical_records_summary`
- `historical_records_policy`
- `historical_records_used_for_current_run`
- `historical_records_usage_scope`
- `reference_issue_set_path`
- `kernel_patch_manifest_path`
- `environment_diagnosis_confirmation_notes`
- `timing_jsonl_path`
- `timing_load_warnings`
- `subagent_invocation_log`
- `execution_authorization_scope`
- `scope_change_confirmation_required`

## 收益口径

- 阶段收益：当前轮相对上一轮已生效配置。
- 累计收益：当前最终配置相对初始基线。
- 单项独立收益：仅当用户明确要求并回退到同一基线独立测试时使用。
- 诊断发现：query mix、workload、硬件规格或测试方法变化导致的差异，不得包装成配置收益。
- 未经用户确认的历史日志、历史报告或旧轮次结果不得计入收益表，不得作为 `selected_optimization_actions` 或单 skill 停止依据。
- 收益表中的每条数据必须能追溯到同一个 `current_run_id`；run_id 缺失或不一致时不得输出收益百分比。

## 阻塞报告要求

当流程阻塞在环境诊断确认、服务健康、目标实例身份、基线确认或权限门控时，报告必须：

- 在前置章节展示 `overall_progress` 和 `workflow_gate_status`。
- 标明 `blocked_gate`、失败命令或检查项、证据路径和用户可执行的修复建议。
- 若阻塞在环境诊断确认，必须展示 `environment_diagnosis`、`environment_diagnosis_confirmation_status` 和用户需确认/修复/补采的最小项。
- 将 `bottleneck_classification`、`candidate_skill_list`、`per_skill_iteration_state` 标为 `not_entered` 或空值。
- 明确说明未执行真实优化动作、无需或需要哪些还原动作。
- 不得把历史记录整理成最终优化结论；历史记录只能出现在“待用户确认的外部材料”章节。

当 `current_evidence_status != current`、`current_run_id` 缺失或证据 run_id 不一致时，也必须按阻塞报告处理。报告必须说明：

- 当前证据状态和失败原因。
- 哪些证据缺失、过期或混入历史产物。
- 需要补采的最小证据。
- 所有优化结论、收益和候选动作均未进入正式状态。

## 报告自检

报告输出前必须确认：

- 环境信息和软硬件信息完整。
- 测试组网和测试用例信心已给出。
- workflow trace 能解释从瓶颈识别、性能采集到候选 skill 列表生成的过程。
- `candidate_skill_list` 已区分 `evidence_candidate` 和 `coverage` 阶段；所有主优化 skill 均有完成、停止或阻塞结论。
- `current_run_id`、`current_run_started_at`、`current_run_manifest` 和 `current_evidence_status` 已给出。
- 环境诊断已给出；若历史 reference 问题集或内核补丁清单不存在，已明确标记 skipped/unknown。
- BIOS 高性能配置、perf/PMU 采集能力和内核补丁齐全性没有证据时，已标记 degraded/unknown 而不是臆测通过。
- perf/PMU 不可用、权限不足、容器/虚拟机未映射采集能力时，已提前告知用户受影响采集项和修复建议。
- 服务健康检查状态和目标实例身份状态已给出；若失败，已阻塞且没有继续进入下游调优。
- 当前证据状态为 `current` 才允许报告正式瓶颈、收益和优化动作。
- 历史记录是否已由用户确认可用已明确；未确认时未用于收益和调优结论。
- 每项优化有验证效果、耗时、风险和回退说明；每个已执行或已分析 skill 都能在 `per_skill_timing_summary` 或 `optimization_timing_details` 中看到耗时。
- 完成态报告必须展示 `workflow_execution_plan`、`workflow_stage_trace`、`agent_timing_summary`、`per_skill_timing_summary`、`optimization_timing` 和 `optimization_timing_details`；缺失时不得作为最终报告交付。
- 完成态报告必须能从 `subagent_invocation_log` 追溯每个候选和 coverage skill 的分析 subagent；执行过真实或 dry-run 验证的轮次必须能追溯执行验证 subagent。
- 单 skill 停止条件和全局停止原因已记录。
- review、还原和案例归档状态已记录。
