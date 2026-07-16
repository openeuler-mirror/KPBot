# Dynamic Workflows Reference

本文是架构图对应的可执行 workflow。旧 CPU 深挖流程作为候选 skill 列表中的 CPU 相关分支保留，不再作为唯一主线。

## 运行时模型

1. 读取顶层架构、输入契约、用户交互门控和检查清单。
2. 从用户输入中抽取 `scenario_environment_summary`，展示给用户确认。确认前不得进入环境备份。
3. 询问并确认用户是否已提供应用场景信息、环境信息和压测/部署信息。
4. 询问并记录调优入口模式：`baseline_first`、`running_app` 或 `historical_baseline_review`；提问必须包含每个模式的介绍、适用场景和后续门控。
5. 确认 Agent 可选操作与变更边界；提问必须解释 `analysis_only`、`dry_run`、`approved_execute` 的权限差异。
6. 初始化本轮 `current_run_id`、`current_run_manifest`、`workflow_execution_plan`、`workflow_stage_trace`、`timing_jsonl_path` 和证据新鲜度策略。
7. 多节点场景先确认 `node_inventory`；容器场景先确认 `container_targets` 和 `container_execution_mode`。
8. 环境备份前逐个物理节点询问是否采集 BIOS 配置；如需采集，继续询问 BMC/IPMI/Redfish 账号和密码/token；如跳过，也必须由用户明确确认。
9. 对每个节点执行环境备份；容器目标同时采宿主机视角和容器内视角。
10. 对每个节点执行环境诊断。
11. 向用户展示环境诊断结果并获得确认。
12. 执行服务健康检查和目标实例身份确认。
13. 在目标规格下建立基线。
14. 向用户展示基线数据并获得确认。
15. 运行性能采集工具识别瓶颈。
16. 基线确认后抓取按进程火焰图、热点函数、热点 DSO/so、进程/线程、topdown L1 icache miss、L3/LLC cache miss、上下文切换等深度证据。
17. 根据采集信息生成 `candidate_skill_list`。
18. 为证据命中的候选 skill 逐个启动分析 subagent，合并候选池后，在已确认授权边界内启动执行验证 subagent 分轮实施、验证、统计收益、记录耗时和判断停止。
19. 候选 skill 完成后，对未进入候选列表的主优化 skill 做 coverage 执行；coverage skill 同样必须启动分析 subagent 并形成结果 JSON，确保所有主优化 skill 均有结论。
20. 若所有主优化 skill 均完成、停止或阻塞并说明原因，先校验报告输入，确保 workflow 阶段轨迹和耗时字段齐全，再输出报告。
21. 执行 review、环境还原和案例归档。

## 总体进度标识

主流程必须维护 `overall_progress`，并在每次进入、完成或阻塞门控时向用户展示。

最低字段：

- `status`：`running` / `blocked` / `completed` / `degraded`。
- `current_gate`：当前门控名。
- `completed_gates`：已完成门控数。
- `total_gates`：本轮计划门控总数。
- `blocked_gate`：阻塞门控，未阻塞时为空。
- `next_gate`：下一门控。
- `gate_status`：各门控状态列表。

推荐门控顺序：

1. `skill_entry_loaded`
2. `startup_input_confirmed`
3. `optimization_entry_mode_confirmed`
4. `scenario_input_recorded`
5. `agent_action_confirmed`
6. `current_run_initialized`
7. `bmc_redfish_prompted`
8. `environment_backup_created`
9. `environment_diagnosis_completed`
10. `environment_diagnosis_confirmation`
11. `service_health_check`
12. `target_instance_identity_checked`
13. `baseline_run`
14. `baseline_confirmation_status`
15. `bottleneck_classification`
16. `evidence_snapshot_collected`
17. `candidate_skill_list`
18. `per_skill_iteration_state`
19. `final_report_path`
20. `review_restore_archive`

同时必须维护 `workflow_stage_trace`。每条记录最低字段：

- `phase`：`phase1_input_env` / `phase2_evidence_routing` / `phase3_iteration_report`。
- `gate`：门控名。
- `status`：`running` / `completed` / `blocked` / `degraded` / `skipped`。
- `started_at`、`ended_at`、`duration_seconds`。
- `evidence_path`：该阶段产物或日志路径。
- `confirmation_status`：该阶段需要用户确认时填写。

`workflow_stage_trace` 必须实时更新并向用户展示关键阶段，不得在报告阶段凭记忆补写。

## 本轮运行身份与证据新鲜度

场景输入和 Agent 操作边界确认后，必须创建本轮运行身份，禁止直接复用历史输出目录作为当前运行。

最低字段：

- `current_run_id`：本轮唯一 ID，建议包含场景名和时间戳。
- `current_run_started_at`：本轮启动时间。
- `current_run_manifest`：本轮输出目录、证据目录、基线路径、任务包路径和报告路径索引。
- `current_evidence_status`：`current` / `missing` / `stale` / `mixed` / `invalid`，在深度证据采集前可为 `missing`，进入候选 skill 列表生成前必须为 `current`。
- `current_evidence_paths`：本轮现场采集证据路径。
- `evidence_freshness_policy`：本轮结论只能引用 `current_run_id` 一致、采集时间不早于 `current_run_started_at`、目标实例身份一致的证据。

硬规则：

- 没有 `current_run_id` 的文件只能归入历史记录，不能作为当前基线、瓶颈或收益结论。
- `current_evidence_status != current` 时，不得进入候选 skill 列表生成、候选动作生成或收益统计。
- 发现历史产物与当前产物冲突时，当前异常优先；报告必须展示当前失败状态，而不是引用历史成功结论。
- 若证据 run_id 不一致、采集时间早于本轮启动时间、目标 PID/容器/端口不一致，必须设置 `overall_progress.status=blocked`，`blocked_gate=evidence_freshness_check`。

## Claude Code Dynamic Workflow 映射

Claude Code 运行时必须维护 `current_workflow_state`，并把本文件的门控映射到动态 workflow：

- `bootstrap`：加载主源、强制 references、用户交互门控和检查清单。
- `scenario-intake`：完成场景摘要、入口模式、节点/容器和操作边界确认。
- `environment-baseline`：完成环境备份、环境诊断、服务健康、目标实例身份和基线确认。
- `bottleneck-detection`：采集性能指标和深度证据，输出 `bottleneck_classification` 与 `performance_signal_summary.json`。
- `candidate-routing`：读取 `candidate-skill-list.md`，生成 `candidate_skill_list`；该列表是动态 workflow 的主要调度来源。
- `candidate-skill-iteration`：只加载候选列表命中的 subskill，串行执行分析和执行验证 subagent。
- `coverage-skill-iteration`：候选完成后加载未命中的主优化 subskill，并形成执行验证或阻塞结论。
- `closeout`：报告、review、还原计划和案例归档。

`current_workflow_state` 至少包含 `current_gate`、`completed_gates`、`blocked_gate`、`next_gate`、`current_run_id`、`evidence_status`、`candidate_skill_list`、`active_workflow` 和 `workflow_trace`。每个 workflow 只能在上游 gate 完成后进入。若证据缺失、权限不足、基线未确认或 run_id 不一致，当前 workflow 必须进入 `blocked` 或 `degraded`，并记录 `blocked_gate`、`fallback_reason` 和下一步补采要求。

## 用户界面层

用户界面层是对话式 UI，不要求图形界面，但必须显式产出以下状态：

- `scenario_environment_summary`：从用户报告中抽取的场景、环境、节点和缺失项摘要。
- `scenario_confirmation_status`：用户是否确认摘要可作为本轮输入。
- `scenario_input_state`：场景、部署、压测、目标规格是否完整。
- `environment_info_input_state`：用户是否已提供环境信息；缺失时由环境备份采集补齐。
- `optimization_entry_mode`：本轮是先建基线、基于已运行应用，还是先审核历史基线。
- `agent_action_confirmation`：当前允许的操作模式和变更范围。
- `overall_progress`：总体进度、当前门控、阻塞门控和下一步。
- `baseline_confirmation_status`：基线是否已由用户确认。
- `environment_diagnosis_confirmation_status`：环境诊断结果是否已由用户确认可继续。
- `metrics_statistics_state`：耗时和收益统计口径是否建立。
- `execution_authorization_scope`：基线确认后允许执行验证 subagent 使用的授权边界。

缺少任一状态时，不得进入真实调优。

启动询问必须按 `user-interaction-gates.md` 执行。涉及模式选择的提问必须包含模式说明，不能只列模式名。若用户选择 `running_app`，后续仍必须完成服务健康检查、目标实例身份确认和基线确认；运行中观测数据未经用户确认不得自动变成正式基线。若用户选择 `historical_baseline_review`，历史材料必须先通过历史记录使用门控。用户未确认摘要、入口模式或操作边界时，不得用“用户已经要求优化”代替确认。

## 多节点与容器优先级

涉及多台机器时，必须先生成并确认 `node_inventory`。每台节点都应执行同一套环境采集与诊断契约：

- `benchmark_client`：采集和诊断压测工具、客户端 CPU/内存/网卡、OS/Kernel、网络链路和客户端是否可能成为瓶颈。
- `application_server` / `database_server`：采集和诊断目标服务、资源约束、容器/虚拟化、应用配置、宿主机硬件、PMU/perf、网卡/IRQ。
- 其他链路节点：采集网络、代理、负载均衡或队列相关信息。

容器目标的优先级：

1. 应用级命令、数据库客户端、配置文件、进程视角、用户态依赖优先在容器内执行。
2. 硬件、内核、PMU/perf、网卡、IRQ、块设备和宿主机 cgroup 视角在宿主机采集。
3. 无法进入容器时，必须记录降级原因，并在环境诊断确认门控让用户确认是否继续。
4. 目标实例身份必须同时包含宿主机 PID/端口和容器内 PID/路径；二者不一致时不得进入基线。

## 环境备份

环境备份必须先于任何优化动作。备份结果不仅用于报告，也用于 Phase 3 环境还原。

执行备份前必须按物理节点询问是否采集 BIOS 配置。用户选择采集时，继续询问 BMC/IPMI/Redfish 地址、账号和密码/token，并可通过 `--bmc-host`、`--bmc-user`、`--bmc-pass` 或 `BMC_HOST`、`BMC_USER`、`BMC_PASS` 传给 `backup_environment.sh`；用户不采集或不提供凭据时记录 `bmc_redfish_collection_status=skipped_by_user` 或 `not_provided`，继续 OS 侧采集。没有得到明确采集或跳过确认时，不得执行备份。

最低输出：

- `environment_backup_dir`
- `per_node_environment_backups`
- `bmc_redfish_collection_status`
- `per_node_bmc_redfish_status`
- `dependency_status`
- `restore_baseline_manifest`

若某类硬件不存在，例如无 GPU/NPU，应记录为 `not_present`，而不是遗漏。

## 环境诊断

环境诊断必须在环境备份后、服务健康检查前执行。规则见 `environment-diagnosis.md`。

最低诊断项：

- 历史 reference 问题集回归检查：存在问题集时检查其中参数或配置是否正常；不存在时标记 `reference_issue_set_status=not_present` 并跳过。
- BIOS 高性能配置检查：检查 Power Profile、C-State、频率策略、NUMA/Node Interleaving、内存频率等是否有高性能证据；证据不足时标记 `bios_performance_status=degraded`。
- Perf/PMU 采集能力检查：检查 perf 命令、PMU 硬件事件、root/capability、`perf_event_paranoid`、`kptr_restrict`、容器/虚拟机采集映射和最小 `perf stat` smoke test；不可用时提前告知用户受影响的性能分析能力。
- 内核补丁齐全性检查：存在补丁 manifest 时逐项验证；不存在时标记 `kernel_patch_status=not_applicable_or_unknown` 并跳过。

最低输出：

- `environment_diagnosis.status`
- `environment_diagnosis.reference_issue_set_status`
- `environment_diagnosis.bios_performance_status`
- `environment_diagnosis.perf_pmu_status`
- `environment_diagnosis.kernel_patch_status`
- `environment_diagnosis.findings`
- `environment_diagnosis.blocked_items`
- `environment_diagnosis.degraded_items`
- `environment_diagnosis.evidence_paths`
- `environment_diagnosis.next_steps`
- `per_node_environment_diagnosis`

若诊断发现阻塞项，应停止在 `environment_diagnosis_completed` 门控并向用户说明问题。若只是证据不足，可继续但必须在报告中标记降级能力。

环境诊断完成后必须进入 `environment_diagnosis_confirmation` 门控。主 Agent 必须向用户展示：

- `environment_diagnosis.status`。
- 阻塞项和降级项。
- BIOS、perf/PMU、内核补丁和历史问题集检查状态。
- 受影响的后续采集能力，例如 perf/topdown/flamegraph 是否可用。
- 证据路径和最小修复/补采建议。

用户确认规则：

- 用户明确确认继续时，写入 `environment_diagnosis_confirmation_status=confirmed`，并可进入服务健康检查。
- 用户要求修复、补采或重新诊断时，写入 `environment_diagnosis_confirmation_status=rebuild_required`，停止在该门控并执行用户要求的补充动作。
- 用户拒绝当前诊断结果时，写入 `environment_diagnosis_confirmation_status=rejected`，不得进入服务健康检查、正式基线或基线性能确认。
- 用户未回复或确认不明确时，保持 `environment_diagnosis_confirmation_status=pending`，不得进入下游门控。

## 服务健康检查与目标实例身份确认

服务健康检查必须在正式基线前完成。它不是性能测试，而是确认“服务可压测、实例可识别、结果可归因”的前置门控。

进入服务健康检查前必须满足：

- `environment_diagnosis.status` 已生成。
- `environment_diagnosis_confirmation_status=confirmed`。

最低检查：

- 端口或入口可达：TCP 端口、HTTP/RPC health endpoint、Unix socket 或队列入口。
- 协议级 smoke test：数据库执行 `SELECT 1`，HTTP 返回 2xx/健康状态，RPC 返回最小请求成功。
- 认证和权限：压测账号、token、证书或 ACL 能从压测端访问目标服务。
- 目标实例身份：端口、PID、容器/进程、二进制、配置文件、数据目录、运行时库、cpuset/资源约束。
- 最小负载 smoke test：1 线程或最小并发短测，确认压测工具能完成初始化并产生有效请求。

失败处理：

- 服务未启动、端口不通、认证失败、目标实例身份不明或 smoke test 失败时，设置 `service_health_status=failed`，`overall_progress.status=blocked`。
- 必须向用户报告具体失败项、证据路径和修复建议。
- 不得进入正式基线、瓶颈识别、候选 skill 列表生成或优化轮次。
- 不得用历史日志、历史报告或旧轮次结果替代现场服务健康检查。

## 基线确认

基线必须在目标规格下建立。用户确认前不得进入候选优化 skill 列表生成。

基线建立前必须已满足：

- `environment_diagnosis_confirmation_status=confirmed`
- `service_health_status=passed`
- `target_instance_identity.status=confirmed`

基线反馈至少包含：

- 基线性能指标。
- CPU、内存、磁盘、网卡、GPU/NPU 利用率。
- 压测命令、并发/线程、运行时长、预热策略和错误率。
- 测试组网信心。
- 测试用例信心。
- 目标规格约束。
- 目标实例身份校验证据。

必须让用户确认基线数据和用例信息是否可作为本轮调优依据。如果用户不确认，写入 `baseline_confirmation_status=rebuild_required`，回退到场景输入、资源约束或基线测试。

用户未确认时，不得采集基线后深度证据，不得识别瓶颈，不得生成候选列表。已有压测结果只能保留为 `baseline_confirmation_status=pending` 的待确认产物。

## 历史记录使用门控

执行过程中发现历史日志、历史报告、旧压测结果或旧优化轮次时，默认只记录为：

- `historical_records_status=discovered_unconfirmed`
- `historical_records_paths`
- `historical_records_summary`

必须先让用户确认这些记录是否属于当前场景、当前目标实例、当前配置和当前测试口径。用户未确认前：

- 不得用历史记录替代服务健康检查或现场基线。
- 不得基于历史记录进入瓶颈识别、候选 skill 列表生成或优化建议。
- 不得把历史收益计入 `improvement_summary`。
- 不得触发单 skill 停止规则。
- 不得把历史记录复制到当前报告的正式结论章节；只能放入“待确认历史材料”章节。

## 瓶颈识别

瓶颈识别输出统一字段 `bottleneck_classification`：

- `disk_bottleneck`
- `network_bottleneck`
- `memory_capacity_bottleneck`
- `memory_bandwidth_bottleneck`
- `cpu_bottleneck`
- `gpu_npu_bottleneck`
- `hardware_capacity_limit`
- `unknown_bottleneck`
- `no_active_bottleneck`

如果分类置信度低，必须先补充采集或输出降级说明；不得直接跳到高风险优化。

若 `environment_diagnosis_confirmation_status != confirmed`、`service_health_status != passed` 或 `baseline_confirmation_status != confirmed`，瓶颈识别状态必须为 `not_entered` 或 `blocked`，不得输出正式 `bottleneck_classification`。

## 基线后深度证据采集

候选 skill 列表生成前，应尽量采集：

- 按目标进程采集火焰图或 perf call stack 数据。
- 按热点函数、DSO/so、进程维度排序，输出热点 so 排名。
- 进程和线程分布。
- topdown 指标，至少包含 L1 icache miss 或可解释降级原因。
- 上下文切换、FE bound、L3/LLC cache miss。

采集结果写入 `evidence_snapshot_dir`，供候选 skill 只读使用。推荐由 `scripts/collect_evidence_snapshot.sh` 生成 `performance_signal_summary.json`：

- `hotspot_function_rank`：按进程/DSO/符号排序的热点函数。
- `hotspot_dso_rank`：按 DSO/so 聚合排序的热点库。
- `topdown.l1_icache_miss_pct`、`topdown.l3_cache_miss_pct`。
- `threading.context_switch_rate_per_sec`。
- `detected_signals`：第三方库热点、网络热点、L1 icache miss 高、L3 miss 高、线程切换高等布尔信号。

若本轮证据缺失或新鲜度校验失败，应输出 `current_evidence_status=missing|stale|mixed|invalid`，并停止在证据门控。此时下游字段必须为：

- `bottleneck_classification=not_entered` 或 `blocked`。
- `candidate_skill_list=[]`。
- `candidate_pool=[]` 或 `{}`。
- `per_skill_iteration_state={}`。

## 候选优化 Skill 列表生成

候选生成规则见 `candidate-skill-list.md`。核心原则：

- 高热点第三方 `.so` 或外部库热点：加入 `performance-library-selection`。
- 网络相关热点函数高：加入 `network-optimization`。
- L1 icache miss 高：加入 `compiler-optimization`，重点分析 PGO/LTO、代码布局、内联和目标架构选项。
- 线程切换高且 L3/LLC miss 高：加入 `cpu-affinity-optimization`。
- 数据库/连接池/队列/缓存/锁等待等应用内部状态解释瓶颈：加入 `application-config-optimization`。
- OS、BIOS、GPU/NPU、硬件规格、Other 等信号命中时，加入对应 skill。
- 未识别瓶颈时仍生成降级报告；不得直接执行高风险调参。

`candidate_skill_list` 可以一次包含多个 skill，但必须记录顺序和原因。执行分两段：

1. `phase=evidence_candidate`：由采集信息命中的候选 skill，优先依次启动分析 subagent。
2. `phase=coverage`：候选完成后，把未进入候选列表的主优化 skill 执行一遍；每个 coverage skill 也必须启动分析 subagent，确保所有主优化 skill 都有分析、验证或阻塞结论。

主 agent 必须维护 `subagent_invocation_log`。只有生成任务包、启动 subagent、收到独立结果 JSON 并通过 run_id/证据新鲜度校验后，某个 skill 才能标记为已分析或已执行。

基线确认后进入 skill 迭代时，不再逐个 skill 或逐轮动作询问用户是否批准。主 agent 和执行验证 subagent 只校验 `execution_authorization_scope`；若动作超出边界，设置 `scope_change_confirmation_required=true` 并集中向用户说明新增权限或风险。

`candidate_skill_list` 是候选 workflow 的主要来源。列表中的每一项至少包含：

- `candidate_id`
- `bottleneck`
- `subskill_name`
- `phase`：`evidence_candidate` / `coverage`
- `priority`
- `reason`
- `required_evidence`
- `skip_reason` 或 `blocked_reason`
- `stop_rule`

未进入 `candidate_skill_list` 的 subskill 不得在候选阶段加载或执行；候选完成后，未命中的主优化 subskill 只能在 coverage 阶段加载，并必须形成分析、执行验证或阻塞结论。

## Skill 迭代停止

每个 skill 独立维护：

- `round_count`
- `recent_gain_pct`
- `consecutive_sub_1pct_rounds`
- `skill_stop_reason`
- `timing_records`

单个 skill 最多尝试 5 轮；若 5 轮收益均小于 1%，停止该 skill，并继续执行 `candidate_skill_list` 中下一个 skill。全局停止条件：

- `no_active_bottleneck`
- `unknown_bottleneck`
- 所有主优化 skill 均完成、停止或阻塞并已说明原因
- 风险预算耗尽
- 用户停止

每个阶段和每轮优化都必须记录耗时。最低记录：

- workflow 门控耗时：写入 `workflow_stage_trace`。
- 每轮优化耗时：写入 `optimization_timing`。
- 每个 skill 或动作的分析/实施/验证耗时：写入 `optimization_timing_details`。
- 按 skill 汇总耗时：写入 `per_skill_timing_summary` 或由报告脚本从 `timing_jsonl_path` 自动汇总。
- 全局耗时汇总：写入 `agent_timing_summary`。

推荐使用 `scripts/record_timing.py --file <timing.jsonl> --stage <gate-or-skill> ...` 追加 JSONL，再在报告输入中汇总。完成态报告缺失任一耗时字段时，必须停在 `report_input_validation`，补齐后再生成报告。

停止单个 skill 后，不直接结束全局流程。主 agent 必须重新评估当前瓶颈、证据状态和候选/coverage 进度：若候选列表仍有未完成 workflow，继续下一个候选；若候选已完成但仍有主优化 skill 未覆盖，进入 coverage workflow；若证据不足，进入 `blocked/degraded` closeout；若所有主优化 skill 均有完成、停止或阻塞结论，进入正式报告。

## 报告、Review 和归档

最终报告后必须执行 review 与归档：

1. review 最终有效配置、回退项、风险、残留工作。
2. 执行或输出环境还原计划。
3. 生成 `case_archive.json`。

归档不是可选项；它是后续案例迭代的输入。
