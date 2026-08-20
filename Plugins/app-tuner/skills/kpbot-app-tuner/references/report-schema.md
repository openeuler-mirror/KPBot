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

## AI 推理场景输入字段

当 `workload_type` 为 `ai_inference` 时，报告必须额外记录以下输入字段（与 `references/input-contract.md` 的 AI 推理字段一致），用于案例归档复现：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ai_inference_subtype` | string | `ai_inference_llm` / `ai_inference_embedding` / `ai_inference_rerank`；未填时默认 `ai_inference_llm` |
| `inference_framework` | string | `vllm` / `sglang` / `tgi` / `trt-llm` |
| `device_type` | string | `npu` / `gpu` / `cpu` |
| `npu_device_ids` | string | NPU 设备 ID 列表（逗号分隔），如 `"0"` 或 `"0,1"` |
| `gpu_device_ids` | string | GPU 设备 ID 列表（逗号分隔） |
| `tensor_parallel_size` | integer | TP 并行度（与 `deployment_topology.tensor_parallel_size` 一致） |
| `pipeline_parallel_size` | integer | PP 并行度（与 `deployment_topology.pipeline_parallel_size` 一致） |
| `model_name` | string | 模型名称，如 `qwen2.5-1.5b` |
| `quantization` | string | `none` / `w8a8` / `w4a16` / `gptq` / `awq` |

## AI 推理指标字段

AI 推理场景（vLLM / SGLang / TGI on Ascend / GPU）必须在以下现有字段中扩展 AI 推理专用子字段。未涉及 AI 推理场景时，这些子字段为空数组或 null，不影响通用报告。

### baseline_metrics 扩展

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `output_throughput_tokps` | number | output tokens/s（解码阶段输出吞吐）|
| `ttft_ms` | number | Time To First Token（ms）|
| `tpot_ms` | number | Time Per Output Token（ms）|
| `e2e_ms` | number | 端到端请求延迟（ms）|
| `npu_utilization_pct` | number | NPU 平均利用率（%）；GPU 场景用 `gpu_utilization_pct` |
| `hbm_bandwidth_pct` | number | HBM 内存带宽利用率（%）|
| `kv_cache_hit_rate` | number | KV cache 命中率（0-1）|
| `batch_utilization` | number | 实际 batch / max batch 占比（0-1）|

`baseline_metrics` 在 AI 推理场景下的示例：

```json
{
  "output_throughput_tokps": 1820.5,
  "ttft_ms": 42.3,
  "tpot_ms": 18.7,
  "e2e_ms": 1280.4,
  "npu_utilization_pct": 78.2,
  "hbm_bandwidth_pct": 64.1,
  "kv_cache_hit_rate": 0.91,
  "batch_utilization": 0.85
}
```

### improvement_summary 扩展

`improvement_summary` 在 AI 推理场景下必须按上述指标的前后对比输出：

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `output_throughput_tokps` | object | `{baseline, optimized, delta_pct}` |
| `ttft_ms` | object | `{baseline, optimized, delta_pct}`；负 delta_pct 表示改善（降低）|
| `tpot_ms` | object | `{baseline, optimized, delta_pct}` |
| `e2e_ms` | object | `{baseline, optimized, delta_pct}` |
| `npu_utilization_pct` | object | `{baseline, optimized, delta_pct}` |
| `hbm_bandwidth_pct` | object | `{baseline, optimized, delta_pct}` |
| `kv_cache_hit_rate` | object | `{baseline, optimized, delta_pct}` |
| `batch_utilization` | object | `{baseline, optimized, delta_pct}` |
| `multi_metric_tradeoff` | boolean | 是否存在 tokens/s ↑ 但 TTFT/TPOT 恶化的权衡场景 |

### environment_snapshot 扩展

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `npu_topology` | object | `{ascend_chip, die, hccs_links}`；GPU 场景用 `gpu_topology` |
| `cann_version` | string | CANN 工具链版本（Ascend 专用）|
| `torch_npu_version` | string | torch_npu 版本（Ascend 专用）|
| `vllm_version` | string | vLLM 版本；其他框架对应字段如 `sglang_version` / `tgi_version` |

### deployment_topology 扩展

当 `deployment_topology` 为 object 时，AI 推理场景追加以下子字段：

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `tensor_parallel_size` | integer | Tensor Parallel 并行度（与 `input-contract.md` 字段名一致） |
| `pipeline_parallel_size` | integer | Pipeline Parallel 并行度（与 `input-contract.md` 字段名一致） |
| `dp_degree` | integer | Data Parallel 并行度 |
| `hccl_comm_group` | string | HCCL 通信组配置（Ascend 专用）；GPU 场景用 `nccl_comm_group` |

### workflow_stage_trace 扩展

AI 推理场景下，`workflow_stage_trace` 必须按 `round_N` 粒度记录每轮轨迹，每轮条目包含：

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `round_id` | integer | 轮次编号（1-based）|
| `round_started_at` / `round_ended_at` | string | 轮次起止时间 |
| `round_duration_seconds` | number | 轮次总耗时 |
| `round_status` | string | `running` / `adopted` / `rejected` / `blocked` |
| `round_metrics_before` / `round_metrics_after` | object | 该轮前后指标快照（含上述 8 个 AI 推理指标）|
| `round_evidence_paths` | string[] | 该轮原始日志路径 |
| `attribution_method` | string | 该轮归因方法，见下方枚举 |

### attribution_method 枚举扩展

`attribution_method` 字段在原通用枚举基础上增加 AI 推理耦合变量归因：

| 枚举值 | 含义 | 使用场景 |
|--------|------|---------|
| `single_variable_round` | 单变量轮次（原有）| 通用单变量优化 |
| `baseline_reset` | 回基线验证（原有）| 用户要求独立收益 |
| `merged_unresolvable` | 合并不可拆分（原有）| 通用合并场景 |
| `confounded` | 存在混淆因子（原有）| 通用混淆 |
| `coupled_variable_group` | 耦合变量组 | **新增**：AI 推理弱耦合协同对（如 OMP_NUM_THREADS+OMP_WAIT_POLICY、gc_threshold+max_split_size）在同一轮次合并执行，组内变量无法独立归因 |

> **注意**：强耦合（互斥对，如 TQE 与 async-scheduling）只取生效的一个，按 `single_variable_round` 处理，不使用本枚举值。

当 `attribution_method=coupled_variable_group` 时，必须同时提供：

- `coupled_variable_group_name`：耦合组名称（如 `omp_passive_synergy`、`gc_split_synergy`）
- `coupled_variables`：组内变量列表
- `coupling_synergy_gain_pct`：协同增量收益（弱耦合复测后填写）

### config_diff_per_round 字段

新增 `config_diff_per_round` 数组，记录每轮相对上一轮已采纳配置的 diff，用于案例归档复现：

```json
[
  {
    "round_id": 1,
    "skill_name": "cpu-affinity-optimization",
    "config_key": "cpu_affinity_mask",
    "config_before": "0-95",
    "config_after": "0-47",
    "change_type": "online",
    "attribution_method": "single_variable_round",
    "adopted": true
  },
  {
    "round_id": 8,
    "skill_name": "application-config-optimization",
    "config_key": "OMP_NUM_THREADS+OMP_WAIT_POLICY",
    "config_before": "OMP_NUM_THREADS=4;OMP_WAIT_POLICY=ACTIVE",
    "config_after": "OMP_NUM_THREADS=8;OMP_WAIT_POLICY=PASSIVE",
    "change_type": "restart",
    "attribution_method": "coupled_variable_group",
    "coupled_variable_group_name": "omp_passive_synergy",
    "coupled_variables": ["OMP_NUM_THREADS", "OMP_WAIT_POLICY"],
    "coupling_synergy_gain_pct": 1.2,
    "adopted": true
  }
]
```

| 子字段 | 类型 | 说明 |
|--------|------|------|
| `round_id` | integer | 轮次编号 |
| `skill_name` | string | 该轮所属 skill |
| `config_key` | string | 变更的配置项；耦合组用 `+` 连接 |
| `config_before` / `config_after` | string | 变更前后值 |
| `change_type` | string | `online` / `restart` / `rebuild` / `firmware` |
| `attribution_method` | string | 归因方法枚举 |
| `coupled_variable_group_name` | string | 耦合组名称（耦合组必填）|
| `coupled_variables` | string[] | 组内变量（耦合组必填）|
| `coupling_synergy_gain_pct` | number | 协同增量收益（弱耦合必填）|
| `adopted` | boolean | 该轮是否采纳 |

### 报告自检补充（AI 推理场景）

当 `workload_type` 为 AI 推理（vLLM / SGLang / TGI 等）时，报告输出前额外确认：

- `baseline_metrics` 包含全部 8 个 AI 推理指标，且预热完成后再采集。
- `improvement_summary` 已按多指标权衡判定，矛盾场景已标注 `multi_metric_tradeoff`。
- 回退阈值遵循 `references/iteration-execution.md` 的统一规则：通用配置轮 `≤0%` 回退 / `0-1%` 噪声区 / `>1%` 保留，二进制替换场景噪声阈值为 `>2%`；不得引用任何未定义的"AI 专用 5%"阈值。
- 耦合变量组轮次已标记 `attribution_method=coupled_variable_group`，并填写耦合组名称和协同收益。
- `config_diff_per_round` 完整记录每轮变更，可独立复现。
- `workflow_stage_trace` 按 `round_N` 粒度记录，每轮含前后指标快照。
