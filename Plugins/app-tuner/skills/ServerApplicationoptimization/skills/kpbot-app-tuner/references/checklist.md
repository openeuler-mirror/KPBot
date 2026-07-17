# 执行检查清单 / Checklist

本清单按架构图自顶向下排列。任何下游步骤开始前，上游门控必须完成。

## Phase 1：用户界面与环境基座

### 用户界面层

- 是否已初始化并持续展示 `overall_progress`：当前门控、已完成门控、阻塞门控和下一步。
- 是否已初始化并持续展示 `workflow_execution_plan` 与 `workflow_stage_trace`：当前 phase/gate、已完成阶段、下一阶段、阶段耗时和证据路径。
- 是否已按 `user-interaction-gates.md` 询问用户是否已提供当前应用场景信息、环境信息、部署/压测信息和权限说明。
- 如果用户提供测试报告或历史材料，是否已抽取 `scenario_environment_summary`，概括场景、环境、多节点、容器、压测命令、历史结果和缺失项，并让用户显式确认。
- 是否已记录 `scenario_confirmation_status=confirmed`；如果未确认，是否停止在 `scenario_confirmation`。
- 是否已记录 `scenario_input_state`、`environment_info_input_state` 和 `missing_input_items`。
- 多节点场景是否已生成并确认 `node_inventory`，包括客户端、服务端和其他链路节点的角色、地址、登录用户和采集范围。
- 是否已询问并记录 `optimization_entry_mode`：`baseline_first`、`running_app` 或 `historical_baseline_review`。
- 若用户选择 `running_app`，是否明确后续先做服务健康检查和目标实例身份确认，未经确认不得把运行中观测数据作为正式基线。
- 若用户选择 `historical_baseline_review`，是否先进入历史记录使用确认门控，未经确认不得替代现场基线。
- 是否已记录测试场景输入：应用、workload、部署组网、压测方法、目标规格。
- 是否已确认 Agent 可选操作：`analysis_only`、`dry_run`、`approved_execute`。
- 是否已明确变更范围：应用配置、网络、OS、BIOS、编译、性能库、硬件建议。
- 是否已明确重启、系统重启、重编译、远程执行权限。
- 是否已定义数据统计口径：Agent 调优耗时统计、各手段提升比例、阶段收益、累计收益。

### 本轮运行身份

- 是否已生成 `current_run_id` 和 `current_run_started_at`。
- 是否已生成或计划生成 `current_run_manifest`，并记录本轮输出目录、基线、证据、任务包、候选池和报告路径。
- 是否确认所有当前结论都引用本轮 `current_run_id`。
- 是否把没有 run_id、run_id 不一致或采集时间早于本轮启动时间的文件标记为历史记录。
- 若 run_id 或证据新鲜度校验失败，是否设置 `current_evidence_status=missing|stale|mixed|invalid` 并停止下游调优。

### 环境备份

- 是否已在备份前按物理节点逐台询问用户是否采集 BIOS 配置；用户选择采集时，是否继续询问 BMC/IPMI/Redfish 地址、账号、密码/token 或环境变量传递方式。
- 是否已记录 `bmc_redfish_collection_status`、`per_node_bmc_redfish_status`、`bmc_host_provided` 和 `bmc_credentials_source`。
- 是否确认所有物理节点的 BMC/Redfish 状态都不是 `not_asked`；用户选择跳过时是否有显式确认。
- 用户不采集 BIOS 配置或未提供 BMC/Redfish 凭据时，是否继续 OS 侧环境备份，并把 BIOS 设置证据不足标记为降级而非臆测通过。
- 是否确认 BMC 密码/token 未写入报告、manifest、日志摘要或案例归档。
- 是否已对每台节点执行或计划执行相同契约的 `scripts/backup_environment.sh <output-dir>`，并生成 `per_node_environment_backups`。
- 是否已采集 CPU/NUMA、内存、磁盘、网卡、GPU/NPU、BIOS、OS、Kernel、容器/虚拟化、编译器、运行时信息。
- 容器目标是否同时采集宿主机视角和容器内视角；应用配置、进程和数据库状态是否优先在容器内采集。
- 无法进入容器时，是否记录 `container_access_status`、失败证据、降级范围和用户确认。
- 是否已记录当前应用配置、启动命令、关键环境变量和运行时库。
- 是否已生成 `restore_baseline_manifest`。
- 是否已记录工具依赖、缺失工具、权限限制和降级能力。

### 环境诊断

- 是否已在环境备份后执行环境诊断，并生成 `environment_diagnosis`。
- 多节点场景是否已为每个节点生成 `per_node_environment_diagnosis`，客户端与服务端都包含工具依赖、OS/Kernel、CPU/NUMA、内存、磁盘、网卡、容器/虚拟化和权限诊断。
- 是否汇总跨节点风险，例如客户端压测能力不足、客户端/服务端网卡不一致、链路丢包、服务端 PMU 不可用或容器内证据缺失。
- 若存在历史 reference 问题集，是否检查其中参数或配置是否已恢复正常。
- 若不存在历史 reference 问题集，是否标记 `reference_issue_set_status=not_present` 或 `skipped`，而不是报错。
- 是否检查 BIOS 是否为高性能配置；证据不足时是否标记 `bios_performance_status=degraded` 并列出需补充的 BMC/Redfish/人工证据。
- 是否检查 `perf` 命令和 PMU 硬件事件当前环境是否可用。
- 是否检查当前用户是否 root 或具备 perf 所需 capability；非 root 受限时是否提前告知用户。
- 是否检查 `perf_event_paranoid`、`kptr_restrict` 对 perf/topdown/flamegraph/内核符号解析的影响。
- 是否识别宿主机、虚拟机、容器环境；容器或虚拟机未映射 PMU/perf 特性时是否标记降级并给出采集建议。
- 是否执行最小 `perf stat` smoke test，并记录失败输出。
- 是否检查内核补丁是否齐全；无补丁清单时是否标记 `kernel_patch_status=not_applicable_or_unknown` 或 `skipped`。
- 若存在内核补丁清单，是否逐项检查并输出缺失补丁、失败证据和影响范围。
- 若环境诊断存在阻塞项，是否停止在 `environment_diagnosis_completed` 门控并告知用户问题。
- 是否已向用户展示环境诊断结果摘要，包括状态、阻塞项、降级项、证据路径和下一步建议。
- 是否已记录 `environment_diagnosis_confirmation_status`：`pending` / `confirmed` / `rejected` / `rebuild_required` / `blocked`。
- 用户未确认或要求补采/修复时，是否停止在 `environment_diagnosis_confirmation` 门控。
- 用户确认诊断结果前，是否禁止进入服务健康检查、目标实例身份确认、正式基线和基线性能确认。

### 服务健康检查 + 目标实例身份

- 是否确认 `environment_diagnosis_confirmation_status=confirmed` 后才开始 `service_health_check`。
- 是否在正式基线前执行 `service_health_check`。
- 是否确认端口/入口可达，且协议级 smoke test 成功。
- 是否确认压测账号、token、证书或 ACL 可从压测端访问目标服务。
- 是否执行最小负载 smoke test，例如 1 线程短测，并确认压测工具初始化成功。
- 是否完成目标实例身份校验：端口、PID、容器/进程、二进制、配置文件、数据目录、运行时库、资源约束。
- 若服务未启动、端口不通、认证失败、权限不足或 smoke test 失败，是否设置 `overall_progress.status=blocked` 和 `blocked_gate=service_health_check`。
- 服务健康检查失败时，是否停止在该门控并告知用户具体问题、证据路径和修复建议。
- 服务健康检查失败时，是否避免运行正式基线、瓶颈识别、候选 skill 列表生成和优化轮次。

### 基线确认

- 是否确认 `environment_diagnosis_confirmation_status=confirmed` 后才开始正式基线。
- 是否确认 `service_health_status=passed` 后才开始正式基线。
- 是否在目标资源规格下建立基线。
- 是否记录测试组网和测试用例信心。
- 是否向用户展示压测命令、并发/线程、运行时长、预热策略、错误率、目标规格约束和目标实例身份。
- 是否完成目标实例身份校验：端口、PID、容器、二进制、配置文件、数据目录、运行时库。
- 是否向用户反馈基线指标和资源利用率。
- **基线运行期间是否仅采集资源级指标**（CPU %util、内存 RSS、磁盘 IOPS、网络带宽、mpstat/iostat/vmstat 等），**未采集微架构级证据**（perf stat -e cycles,instructions,cache-misses、perf record、火焰图等属于深度证据，必须等基线确认后才采集）。
- 用户是否显式确认基线数据和用例信息可作为本轮调优依据；未确认时是否写入 `baseline_confirmation_status=rebuild_required` 并回退到场景准备、规格约束或基线测试。
- 用户未明确确认时，是否禁止进入瓶颈识别、深度证据采集、候选 skill 列表生成和优化动作。
- 是否写入 `checkpoint_5.json`。

### 历史记录使用确认

- 是否把发现的历史日志、历史报告或旧轮次结果标记为 `historical_records_status=discovered_unconfirmed`。
- 是否先询问用户这些历史记录是否属于当前场景、目标实例、配置和测试口径。
- 用户确认前，是否禁止用历史记录替代服务健康检查、现场基线、瓶颈识别、候选 skill 列表生成或收益统计。
- 用户确认前，是否禁止根据历史记录继续调优或触发单 skill 停止规则。
- 是否禁止把历史成功结论覆盖当前失败状态；当前异常必须优先展示。

## Phase 2：瓶颈识别与候选优化 skill 列表

### 性能采集工具

- 是否采集磁盘、网卡、内存、CPU 指标。
- 若存在 GPU/NPU 等计算卡，是否采集设备利用率、显存/内存、拷贝带宽和设备错误指标。
- 是否判断是否存在硬件规格不足或更换高规格硬件的证据。
- 若工具缺失，是否输出 `degraded_capabilities` 和 `bottleneck_confidence=low`。

### 基线后深度证据

- **是否确认 `baseline_confirmation_status=confirmed` 后才开始深度证据采集**（基线确认是硬门控，未确认前 perf stat -e、perf record、火焰图等微架构级采集一律禁止）。
- 是否按目标进程采集火焰图或 perf call stack 数据；无法生成火焰图时是否说明原因。
- 是否采集热点函数，并按进程/DSO/so/符号排序。
- 是否根据热点函数 so 排名识别高热点第三方库。
- 是否根据热点函数识别 TCP/UDP/socket/epoll/softirq/NAPI/SKB/netfilter/NIC driver 等网络相关热点。
- 是否采集进程/线程分布和上下文切换。
- 是否采集 topdown 指标：L1 icache miss、FE bound、L3/LLC cache miss、backend bound、bad speculation、retiring。
- 是否生成 `performance_signal_summary.json`，包含 `hotspot_function_rank`、`hotspot_dso_rank`、`topdown`、`threading`、`detected_signals`。
- 是否生成 `evidence_snapshot_dir` 和 `snapshot_metadata.json`。
- 是否校验 `evidence_snapshot_dir`、`snapshot_metadata.json`、基线和目标实例身份都属于同一个 `current_run_id`。
- 若证据缺失、过期、混入历史产物或目标实例不一致，是否停止在证据门控并输出补采清单。

### 瓶颈识别

- 是否仅在 `environment_diagnosis_confirmation_status=confirmed`、`service_health_status=passed` 且 `baseline_confirmation_status=confirmed` 后进入瓶颈识别。
- 是否输出统一 `bottleneck_classification`。
- 是否包含支撑证据和置信度。
- 若仍有瓶颈，是否进入候选优化 skill 列表生成。
- 若无可识别瓶颈，是否进入报告输出而不是继续盲目调优。

### 候选优化 skill 列表生成

- 是否读取 `references/candidate-skill-list.md`。
- 是否根据采集信息生成 `candidate_skill_list`，而不是只根据静态瓶颈表选择 skill。
- 高热点第三方库是否把 `performance-library-selection` 加入候选列表。
- 网络相关热点高是否把 `network-optimization` 加入候选列表。
- L1 icache miss 高是否把 `compiler-optimization` 加入候选列表，并标注 PGO/LTO 分析方向。
- 线程切换高且 L3/LLC cache miss 高是否把 `cpu-affinity-optimization` 加入候选列表。
- 是否把未进入证据候选列表的主优化 skill 追加为 `phase=coverage`。
- 每个候选/coverage skill 是否有任务包、证据目录、输出路径和停止条件。
- 若某 skill blocked，是否列出最小补充证据。

## Phase 3：迭代优化、review、还原和归档

### Skill 迭代

- 每个 skill 是否独立记录轮次、收益、风险和是否停止。
- 每个 workflow 阶段、每个 skill、每轮候选动作是否记录分析耗时、实施耗时、验证耗时和总耗时。
- 是否写入 `timing_jsonl_path`、`agent_timing_summary`、`per_skill_timing_summary`、`optimization_timing` 和 `optimization_timing_details`。
- 每轮真实变更前是否校验 `execution_authorization_scope`，且未在授权范围内逐个 skill/逐轮重复询问用户批准。
- 每个候选/coverage skill 是否真实启动分析 subagent；每轮执行验证是否真实启动执行验证 subagent，并写入 `subagent_invocation_log`。
- 每轮是否只允许一个执行主体修改环境。
- 每轮是否只变更一个 skill 的变量（单变量原则）；来自不同 skill 的变更是否拆分为独立轮次。
- cpu-affinity-optimization 是否在所有其他候选 skill 之前执行，且有 `skill_execution_order.cpu_affinity_first_verified=true`。
- 每轮是否执行目标实例身份复核、压测、收益计算和回退判断。
- 单个 skill 是否最多尝试 5 轮。
- 同一 skill 是否在 5 轮收益均小于 1% 时停止。
- 若该 skill 停止，是否继续执行 `candidate_skill_list` 中下一个候选或 coverage skill。
- 是否确认所有主优化 skill 均完成、停止或阻塞并说明原因后才进入最终报告。

### 报告输出

- 是否包含 `overall_progress`、`workflow_gate_status`、当前阻塞门控和下一步。
- 是否包含 `workflow_execution_plan` 和 `workflow_stage_trace`，能让用户看到实际执行流程和阶段状态。
- 若阻塞在服务健康、实例身份、基线确认或权限门控，是否明确输出失败证据而不是交付历史报告式结论。
- 若 `current_evidence_status != current` 或 `current_run_id` 缺失，是否生成阻塞/降级报告而不是优化报告。
- 是否把历史记录限制在“待确认历史材料”章节，未写入正式收益、候选动作或最终结论。
- 是否包含环境信息和软硬件信息。
- 是否包含测试组网和测试用例信心。
- 是否包含调优报告 workflow 流程分析。
- 是否包含瓶颈分析和证据。
- 是否包含优化手段验证效果和耗时。
- 完成态报告是否含非空 `agent_timing_summary`、`optimization_timing`、`optimization_timing_details`；缺失时是否停在 `report_input_validation` 而不是生成最终报告。
- 是否包含结论与下一步计划。
- 是否区分阶段收益、累计收益、诊断发现和 workload 变化。
- 是否每个 skill 都有独立可量化的收益归因，写入 `per_skill_gain_summary`；混淆轮次是否明确标记 `confounded`。

### Review 与环境还原

- 是否 review 最终有效配置、被拒绝动作、回退动作、残留风险和不可实施动作。
- 是否生成或执行环境还原计划。
- 是否确认还原结果或记录人工待执行项。

### 案例归档

- 是否生成 `case_archive.json`。
- 是否归档 `overall_progress`、`service_health_status`、`workflow_gate_status` 和历史记录确认状态。
- 是否归档环境备份、基线、证据、候选池、每轮结果、最终报告、review 和还原结果。
- 是否标注可复用条件、不可复用条件和后续案例检索关键词。
