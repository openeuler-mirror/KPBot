# 输入输出约定 / Input Contract
本文定义服务器应用优化 Agent 的用户界面输入、确认项、候选优化 skill 列表字段和最终输出。字段命名应在任务包、报告和案例归档中保持一致。

## 用户界面输入

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `scenario_description` | string | 是 | 测试场景描述 |
| `application_name` | string | 否 | 应用名称 |
| `workload_type` | string | 否 | `database` / `rpc` / `web` / `batch` / `compute` / `ai_inference` / `unknown` |
| `deployment_topology` | object/string | 否 | 本机压测、远程压测、客户端/服务端组网、容器或虚拟化形态 |
| `benchmark_command` | string | 否 | 压测命令或脚本路径 |
| `benchmark_script_provided` | boolean | 是 | 是否提供测试脚本 |
| `deployment_guide_provided` | boolean | 是 | 是否提供部署指导 |
| `scenario_input_state` | string | 是 | `complete` / `partial` / `missing`，当前应用场景信息是否已提供完整 |
| `environment_info_input_state` | string | 是 | `complete` / `partial` / `missing`，用户是否已提供环境信息；缺失时进入环境备份采集 |
| `missing_input_items` | array | 否 | 启动询问后仍缺失的场景、环境、压测或权限材料 |
| `optimization_entry_mode` | string | 是 | `baseline_first` / `running_app` / `historical_baseline_review` |
| `optimization_entry_mode_confirmation_status` | string | 是 | `pending` / `confirmed` / `rejected`；提问必须包含各模式介绍 |
| `mode_prompt_explanations` | object/array | 否 | 本轮所有模式选择提问中展示过的模式说明，用于审计用户是否看到了模式含义 |
| `target_resource_profile` | string/object | 否 | 目标规格，如 `8U32G`、`16C64G`、GPU/NPU 规格 |
| `target_metrics` | object | 否 | 目标指标，如 TPS/QPS/P95/吞吐/错误率 |
| `evidence_inputs` | array | 否 | 用户提供的 perf、topdown、日志、数据库状态等证据 |
| `reference_issue_set_path` | string | 否 | 历史 reference 问题集路径；不存在时跳过该诊断项 |
| `kernel_patch_manifest_path` | string | 否 | 内核补丁检查清单路径；不存在时补丁齐全性诊断标记为 unknown/skipped |
| `scenario_environment_summary` | object | 是 | Agent 从用户报告/说明中抽取的场景与环境摘要，供用户确认 |
| `scenario_confirmation_status` | string | 是 | `pending` / `confirmed` / `rejected` / `rebuild_required`；确认前不得环境备份或压测 |
| `scenario_confirmation_notes` | string/array | 否 | 用户对场景摘要的确认、修正或补充项 |
| `node_inventory` | array | 否 | 多节点清单，包含 `node_id`、角色、地址、登录用户、物理机/容器宿主关系 |
| `node_inventory_confirmation_status` | string | 否 | `pending` / `confirmed` / `rebuild_required` / `not_applicable` |
| `container_targets` | array | 否 | 容器目标清单，包含容器名/ID、运行时、进入方式、目标 PID 和应用路径 |
| `container_execution_mode` | string | 否 | `container_first` / `host_only_degraded` / `host_and_container` / `not_applicable` |
| `container_access_status` | string | 否 | `confirmed` / `blocked` / `degraded` / `not_applicable` |

## Agent 可选操作确认

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_action_mode` | string | `analysis_only` / `dry_run` / `approved_execute` |
| `change_scope` | string/array | 允许变更范围：`app_config`、`network`、`os`、`bios`、`compiler`、`library`、`hardware_advice` |
| `restart_allowed` | boolean | 是否允许重启服务 |
| `system_reboot_allowed` | boolean | 是否允许系统重启 |
| `rebuild_allowed` | boolean | 是否允许重新编译或替换二进制 |
| `remote_execution_allowed` | boolean | 是否允许 SSH/远程执行 |
| `hardware_change_allowed` | boolean | 是否允许输出或执行硬件规格调整建议 |
| `rollback_required` | boolean | 真实变更前是否必须有回退方案，默认 true |
| `agent_action_confirmation_status` | string | `pending` / `confirmed` / `rejected`；确认前不得执行远程命令或真实变更 |
| `post_baseline_execution_policy` | string | `within_confirmed_scope_no_per_skill_prompt` / `ask_on_scope_change`；基线确认后默认不逐个 skill 询问 |
| `execution_authorization_scope` | object/array | 用户已确认的变更范围、权限、验证窗口、重启/重编译/远程执行/硬件建议边界 |
| `scope_change_confirmation_required` | boolean | 候选动作是否超出已确认边界；为 true 时必须回到用户确认门控 |

## 总体进度与门控

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `overall_progress` | object | 总体进度标识，包含当前门控、已完成门控、阻塞门控和下一步 |
| `overall_progress.status` | string | `running` / `blocked` / `completed` / `degraded` |
| `overall_progress.current_gate` | string | 当前执行门控，如 `service_health_check` |
| `overall_progress.completed_gates` | number | 已完成门控数 |
| `overall_progress.total_gates` | number | 本轮计划门控总数 |
| `overall_progress.blocked_gate` | string | 阻塞门控，未阻塞时为空 |
| `overall_progress.next_gate` | string | 下一门控 |
| `workflow_gate_status` | array | 各门控状态列表，元素至少包含 `step`、`gate`、`status`、`evidence_path` |
| `workflow_execution_plan` | array | 本轮计划执行的 workflow 阶段，按顺序列出 phase、gate、预期产物和确认点 |
| `workflow_stage_trace` | array | 实际执行轨迹，元素至少包含 `phase`、`gate`、`status`、`started_at`、`ended_at`、`duration_seconds`、`evidence_path` |
| `gate_confirmation_log` | array | 所有用户确认门控的提问、答复摘要、状态和写入时间 |
| `environment_diagnosis_confirmation_status` | string | 环境诊断结果用户确认状态：`pending` / `confirmed` / `rejected` / `rebuild_required` / `blocked` / `not_entered` |
| `environment_diagnosis_confirmation_notes` | string/array | 用户对环境诊断结果的确认意见、拒绝原因、要求补采或修复项 |

## 本轮运行身份与证据新鲜度

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `current_run_id` | string | 本轮唯一运行 ID，所有当前产物必须引用该 ID |
| `current_run_started_at` | string | 本轮启动时间，用于判断证据是否早于当前运行 |
| `current_run_manifest` | object/string | 本轮输出目录、基线、证据、任务包、候选池、报告路径索引 |
| `current_evidence_status` | string | `current` / `missing` / `stale` / `mixed` / `invalid` |
| `current_evidence_paths` | array | 本轮现场采集证据路径，不含未确认历史产物 |
| `evidence_freshness_policy` | string/object | 证据必须匹配当前 run_id、目标实例身份和采集时间 |
| `evidence_freshness_failure_reason` | string | 缺失、过期、run_id 不一致、实例身份不一致、混入历史产物等 |
| `evidence_freshness_next_steps` | array/string | 需要补采或修复的最小证据清单 |

## 服务健康检查与目标实例身份

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `service_health_status` | string | `pending` / `passed` / `failed` / `blocked` / `degraded` |
| `service_health_checks` | array/object | 端口、协议、认证、最小查询/请求、1 线程 smoke test 等检查结果 |
| `service_health_evidence` | array/string | 健康检查原始日志路径 |
| `service_health_failure_reason` | string | 服务未启动、端口不通、认证失败、权限不足、实例不明、smoke test 失败等 |
| `service_health_next_steps` | array/string | 服务健康失败时给用户的修复建议 |
| `target_instance_identity.status` | string | `pending` / `confirmed` / `failed` / `ambiguous` |
| `target_instance_identity.evidence_path` | string | 端口、PID、容器、二进制、配置、数据目录、运行时库等证据路径 |

## 基线数据确认

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `baseline_metrics` | object | 基线性能指标 |
| `baseline_raw_logs` | array | 原始日志路径 |
| `baseline_resource_utilization` | object | CPU、内存、磁盘、网卡、GPU/NPU 利用率 |
| `baseline_confirmation_status` | string | `pending` / `confirmed` / `rebuild_required` / `not_entered` / `blocked` |
| `baseline_confirmation_notes` | string/array | 用户确认意见或需重建原因 |
| `test_topology_confidence` | string | `high` / `medium` / `low` |
| `test_case_confidence` | string | `high` / `medium` / `low` |
| `target_instance_identity` | object | 端口、PID、容器、二进制、配置、数据目录等目标实例身份 |

## 历史记录使用确认

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `historical_records_status` | string | `none_found` / `discovered_unconfirmed` / `user_confirmed_usable` / `user_rejected` |
| `historical_records_paths` | array | 发现的历史日志、报告或旧轮次路径 |
| `historical_records_summary` | object/array | 只读摘要；用户确认前不得作为调优依据 |
| `historical_records_user_confirmation` | string/object | 用户是否确认这些历史记录可用于本轮分析 |
| `historical_records_policy` | string | 默认策略：用户确认前不得替代服务健康检查、现场基线、瓶颈识别或收益统计 |
| `historical_records_used_for_current_run` | boolean | 是否被用户确认并用于本轮；默认 false |
| `historical_records_usage_scope` | string/array | 用户确认后的使用范围，例如只做背景参考、对比分析或案例复盘 |

## 环境诊断与备份输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `environment_backup_dir` | string | 环境备份目录 |
| `per_node_environment_backups` | array | 多节点环境备份结果，每个节点一个对象，包含节点角色、备份目录、采集方式、状态 |
| `per_node_environment_diagnosis` | array | 多节点诊断结果，每个节点一个对象，包含诊断摘要、阻塞项、降级项和证据路径 |
| `bmc_redfish_collection_status` | string | `not_asked` / `provided` / `skipped_by_user` / `not_provided` / `failed`，BMC/Redfish BIOS 只读采集状态 |
| `per_node_bmc_redfish_status` | array | 每个物理节点的 BMC/Redfish 询问与采集状态；任一节点 `not_asked` 时不得进入备份 |
| `bmc_host_provided` | boolean | 是否提供 BMC 地址；报告中不得输出敏感凭据 |
| `bmc_credentials_source` | string | `env` / `script_args` / `interactive` / `not_provided`，凭据来源，禁止记录明文密码/token |
| `environment_diagnosis` | object | CPU、内存、磁盘、网卡、GPU/NPU、BIOS、OS、容器等诊断摘要 |
| `environment_diagnosis.status` | string | `passed` / `failed` / `blocked` / `degraded` |
| `environment_diagnosis.reference_issue_set_status` | string | `not_present` / `passed` / `failed` / `degraded` / `invalid` / `skipped` |
| `environment_diagnosis.bios_performance_status` | string | `passed` / `failed` / `degraded` / `unknown` |
| `environment_diagnosis.perf_pmu_status` | string | `passed` / `failed` / `degraded` / `unknown` |
| `environment_diagnosis.perf_pmu_checks` | object | perf 命令、权限、PMU 事件、虚拟化/容器映射、smoke test 检查结果 |
| `environment_diagnosis.perf_pmu_findings` | array | perf/PMU 采集能力发现和失败原因 |
| `environment_diagnosis.kernel_patch_status` | string | `passed` / `failed` / `skipped` / `not_applicable_or_unknown` |
| `environment_diagnosis.findings` | array | 环境诊断发现 |
| `environment_diagnosis.blocked_items` | array | 需要阻塞后续流程的问题 |
| `environment_diagnosis.degraded_items` | array | 证据不足或降级检查项 |
| `environment_diagnosis.evidence_paths` | array | 环境诊断证据路径 |
| `environment_diagnosis.next_steps` | array | 修复或补充证据建议 |
| `environment_diagnosis_confirmation_status` | string | `pending` / `confirmed` / `rejected` / `rebuild_required` / `blocked` / `not_entered`；确认前不得进入服务健康检查和基线性能确认 |
| `environment_diagnosis_confirmation_notes` | string/array | 用户确认继续、要求补采、拒绝当前诊断或修复后的说明 |
| `restore_baseline_manifest` | object/string | 环境还原所需的基线配置、命令和文件路径 |
| `dependency_status` | array/object | 工具依赖状态 |
| `missing_dependencies` | array | 缺失工具或权限 |
| `degraded_capabilities` | array | 降级能力说明 |

## 瓶颈识别输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `bottleneck_classification` | string | `disk_bottleneck` / `network_bottleneck` / `memory_capacity_bottleneck` / `memory_bandwidth_bottleneck` / `cpu_bottleneck` / `gpu_npu_bottleneck` / `hardware_capacity_limit` / `unknown_bottleneck` / `no_active_bottleneck` / `not_entered` / `blocked` |
| `bottleneck_confidence` | string | `high` / `medium` / `low` |
| `bottleneck_evidence` | object | 支撑瓶颈判断的指标和文件路径 |
| `hardware_capacity_recommendation` | object/string | 更换高规格硬件建议，仅在证据充分时输出 |

## 深度证据输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `evidence_snapshot_dir` | string | 统一证据快照目录 |
| `flamegraph_path` | string | 火焰图路径，可为空但需说明原因 |
| `perf_data_path` | string | perf data 路径 |
| `hot_functions` | array/object | 热点函数 |
| `hotspot_function_rank` | array | 按进程/DSO/so/符号排序的热点函数 |
| `hotspot_dso_rank` | array | 按 DSO/so 聚合排序的热点库 |
| `process_thread_summary` | object | 进程/线程分布、热点线程、上下文切换 |
| `topdown_summary` | object | L1 icache miss、FE bound、backend bound、bad speculation、retiring、L3/LLC cache miss 等 |
| `performance_signal_summary_path` | string | `performance_signal_summary.json` 路径 |
| `performance_signal_summary` | object | 候选 skill 列表生成所需采集摘要 |

## 候选优化 Skill 列表输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `candidate_skill_list` | array | 根据采集信息生成的候选优化 skill 列表，包含 `evidence_candidate` 和 `coverage` 阶段 |
| `candidate_skill_list[].phase` | string | `evidence_candidate` / `coverage` |
| `candidate_skill_list[].source_signal` | string | 触发该 skill 的采集信号，例如 `third_party_library_hotspot`、`network_hotspot_high`、`l1_icache_miss_high` |
| `candidate_reason` | object/string | 候选列表生成依据 |
| `dynamic_route_plan` | array | 兼容字段，等价于 `candidate_skill_list`，不再作为主语义 |
| `subagent_task_manifest` | string | 任务包 manifest 路径 |
| `subagent_invocation_log` | array | 每个候选/coverage skill 的 subagent 启动记录，包含 subskill、task_path、subagent_id、status、result_path |
| `candidate_pool` | object/array | 合并后的候选动作池 |
| `selected_optimization_actions` | array | 当前轮被选中的优化动作 |
| `rejected_optimization_actions` | array | 当前轮被拒绝或暂缓动作 |

## Skill 迭代状态

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `per_skill_iteration_state` | object | 每个 skill 的轮次、最近收益、是否停止 |
| `current_round_summary` | object/string | 当前轮优化摘要 |
| `iteration_decision` | string | `continue` / `stop_skill` / `next_candidate_skill` / `stop_global` |
| `iteration_decision_reason` | string | 当前决策原因 |
| `next_round_focus` | array/string | 下一轮重点方向 |
| `effective_config_history` | array | 分轮次已生效配置历史 |

## 数据统计输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_timing_summary` | object | Agent 调优总耗时、分析/实施/验证耗时 |
| `per_skill_timing_summary` | array | 按 `skill_name` 汇总的分析、实施、验证和总耗时 |
| `optimization_timing` | array | 每轮优化耗时记录 |
| `optimization_timing_details` | array | 各优化手段耗时明细 |
| `improvement_summary` | array/object | 各手段提升比例、阶段收益和累计收益 |
| `timing_jsonl_path` | string | 结构化计时 JSONL 路径，推荐由 `scripts/record_timing.py` 追加 |

`optimization_timing[]` 和 `optimization_timing_details[]` 的每条记录至少包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stage` | string | workflow 阶段或优化项名称 |
| `skill_name` | string | 所属 skill，非 skill 阶段可为空 |
| `round_name` | string | 轮次名，如 `baseline`、`round-1` |
| `status` | string | `completed` / `blocked` / `rejected` / `degraded` |
| `analysis_seconds` | number | 分析耗时 |
| `implementation_seconds` | number | 实施耗时 |
| `validation_seconds` | number | 验证耗时 |
| `total_seconds` | number | 总耗时 |
| `evidence_path` | string | 计时对应证据路径 |

## 最终输出

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `final_report_path` | string | 最终报告路径 |
| `review_result` | object | review 结论、残留风险、最终有效配置 |
| `restore_result` | object | 环境还原结果或待人工执行项 |
| `case_archive_path` | string | 归档案例路径 |
| `global_stop_reason` | string | `unidentified_bottleneck` / `all_skills_completed` / `risk_budget_exhausted` / `user_stopped` |
## 兼容旧字段

旧字段 `baseline_targets`、`analysis_findings`、`optimization_actions`、`cumulative_validation_summary` 继续允许出现在报告中，但新任务包和案例归档应优先使用本文字段。
