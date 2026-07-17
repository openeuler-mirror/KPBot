# 服务器应用优化 Agent 架构映射

本文把架构图中的模块映射为 skill 可执行的三阶段流程。主入口 `SKILL.md` 只保留编排规则，本文件作为顶层架构契约使用。

## Phase 1：用户界面与环境基座

目标：让优化任务在进入采集和调优前先形成稳定输入、确认边界和可回退环境。

| 架构图模块 | Skill 承接 |
|---|---|
| 测试场景输入 | `scenario_description`、`application_name`、`workload_type`、`benchmark_command`、`deployment_topology` |
| 启动输入询问 | `scenario_input_state`、`environment_info_input_state`、`missing_input_items`、`optimization_entry_mode` |
| 场景摘要确认 | `scenario_environment_summary`、`scenario_confirmation_status` |
| 多节点清单确认 | `node_inventory`、`node_inventory_confirmation_status` |
| 容器目标确认 | `container_targets`、`container_execution_mode`、`container_access_status` |
| Agent 可选操作确认 | `agent_action_mode`：`analysis_only` / `dry_run` / `approved_execute` |
| 总体进度标识 | `overall_progress`、`workflow_gate_status` |
| Workflow 阶段轨迹 | `workflow_execution_plan`、`workflow_stage_trace` |
| 本轮运行身份 | `current_run_id`、`current_run_started_at`、`current_run_manifest`、`current_evidence_status` |
| 备份后环境诊断 | `environment_diagnosis`、`reference_issue_set_status`、`bios_performance_status`、`perf_pmu_status`、`kernel_patch_status` |
| BMC/Redfish 只读采集确认 | `bmc_redfish_collection_status`、`bmc_host_provided`、`bmc_credentials_source` |
| 环境诊断结果确认 | `environment_diagnosis_confirmation_status`、`environment_diagnosis_confirmation_notes` |
| 服务健康检查 | `service_health_status`、`service_health_checks`、`service_health_evidence` |
| 目标实例身份确认 | `target_instance_identity`、`target_instance_identity.status` |
| 基线数据确认 | `baseline_confirmation_status`：`pending` / `confirmed` / `rebuild_required` |
| 历史记录确认 | `historical_records_status`、`historical_records_user_confirmation` |
| 数据统计 | `agent_timing_summary`、`optimization_timing`、`improvement_summary` |
| 环境诊断 + 环境备份 | `environment_backup_dir`、`environment_diagnosis`、`restore_baseline_manifest` |
| 多节点环境基座 | `per_node_environment_backups`、`per_node_environment_diagnosis`、`per_node_bmc_redfish_status` |

Phase 1 完成标志：

- 场景、组网、压测方法、目标规格、变更边界已记录。
- 用户已确认场景与环境摘要；不能从测试报告直接进入备份或基线。
- 多节点清单已确认；客户端、服务端和关键链路节点均进入同一环境采集/诊断契约。
- 容器目标已确认；应用侧采集和配置操作优先在容器内执行，宿主机负责硬件/内核/PMU/网卡证据。
- 已确认用户是否提供应用场景信息、环境信息、部署/压测信息，并记录调优入口模式。
- `overall_progress` 已初始化，并能展示当前门控、阻塞门控和下一步。
- `workflow_execution_plan` 与 `workflow_stage_trace` 已初始化并随阶段更新。
- `current_run_id` 已生成，所有当前产物都能追溯到本轮运行身份。
- 环境备份后已完成环境诊断：历史 reference 问题集回归、BIOS 高性能配置、perf/PMU 采集能力、内核补丁齐全性。
- 环境备份前已询问是否采集 BIOS 配置；用户不采集或不提供 BMC 凭据时已记录跳过和 BIOS 诊断降级原因。
- 多节点场景已逐台询问 BMC/Redfish 采集或跳过，且无节点停留在 `not_asked`。
- 环境诊断结果已展示给用户并获得确认；`environment_diagnosis_confirmation_status=confirmed`。
- 服务健康检查通过，目标实例身份可确认。
- 基线在目标规格下建立并经用户确认。
- 本轮运行身份和证据新鲜度策略已就位；深度证据采集前 `current_evidence_status` 可为 `missing`，进入候选 skill 列表生成前必须为 `current`。
- 缺失、过期、混入历史产物或 run_id 不一致的证据不得进入正式瓶颈结论、候选 skill 列表和收益统计。
- 发现历史记录时，已获得用户确认或明确标记为未确认且不参与调优。
- 环境备份可用于 review 和还原。
- `agent_timing_summary`、`optimization_timing`、`optimization_timing_details` 的统计口径已建立。

## Phase 2：性能信息采集与候选 skill 列表

目标：先识别瓶颈，再根据本轮采集信息生成候选优化 skill 列表。候选 skill 优先执行，随后执行未命中的 coverage skill，最终所有主优化 skill 都要有结论。

| 采集信息或瓶颈类型 | 候选 skill |
|---|---|
| 高热点第三方 `.so` 或外部库函数 | `performance-library-selection` |
| 网络相关热点函数高 | `network-optimization` |
| topdown L1 icache miss 高 | `compiler-optimization`，重点分析 PGO/LTO |
| 线程切换高且 L3/LLC cache miss 高 | `cpu-affinity-optimization` |
| `disk_bottleneck` | 先输出磁盘瓶颈报告；如有应用刷盘/缓存问题，加入应用配置 skill |
| `network_bottleneck` | 网络参数调优 skill |
| `memory_capacity_bottleneck` | 应用配置、OS、硬件规格分析 |
| `memory_bandwidth_bottleneck` | CPU 亲和性、NUMA、应用配置、硬件规格分析 |
| `cpu_bottleneck` | CPU 亲和性、应用配置、性能库、编译、BIOS、OS、Other |
| `gpu_npu_bottleneck` | GPU/NPU 等计算卡 skill |
| `hardware_capacity_limit` | 更换高规格硬件分析 |
| `unknown_bottleneck` | 补充采集或输出无法识别瓶颈报告 |

基线确认后必须补充采集：

- 火焰图或 perf data。
- 热点函数和热点 DSO/so 排名。
- 进程/线程分布。
- topdown 指标，包括 L1 icache miss、FE bound、L3/LLC cache miss、上下文切换等。

Phase 2 完成标志：

- 已生成 `bottleneck_classification`。
- 已生成 `performance_signal_summary.json` 或明确降级原因。
- 已生成 `candidate_skill_list`，包含 `evidence_candidate` 和 `coverage` 阶段。
- 每个候选 skill 都有任务包、证据目录和预期输出。

## Phase 3：迭代、报告、review 和归档

目标：把优化动作变成可验证、可回退、可复用的案例。

| 架构图模块 | Skill 承接 |
|---|---|
| 单 skill 5 轮收益均 <1% 停止 | `per_skill_iteration_state` |
| 无可识别瓶颈或所有调优完成 | `global_stop_reason` |
| 输出报告 | `final_report_path` |
| review & 环境还原 | `review_result`、`restore_result` |
| 数据归档 | `case_archive_path`、`case_archive.json` |

Phase 3 完成标志：

- 报告覆盖环境、组网信心、workflow、瓶颈、优化效果、耗时和下一步。
- review 确认最终配置、回退项、风险和残留工作。
- 案例归档可被后续任务检索复用。

## 架构约束

- 用户确认先于变更执行。
- 环境备份先于优化。
- 本轮运行身份和证据新鲜度校验先于基线、瓶颈和收益结论。
- 环境诊断先于服务健康检查和基线建立。
- 环境诊断结果用户确认先于服务健康检查、目标实例身份确认和基线建立。
- 服务健康检查和目标实例身份确认先于基线建立。
- 基线确认先于深度证据采集。
- 瓶颈识别和性能信息采集先于候选 skill 列表生成。
- 未经用户确认的历史记录不得替代服务健康检查、现场基线、瓶颈识别或收益统计。
- skill 分析先于实施。
- review 和还原先于案例归档。
