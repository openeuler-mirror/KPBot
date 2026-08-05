# Subagent Orchestration

候选 skill workflow 使用两类 subagent 执行 `candidate_skill_list` 中的 skill：分析 subagent 和执行验证 subagent。主 agent 只维护全局状态、候选 skill 列表、任务包、候选池、收益统计和最终决策。

## 职责边界

### 主 agent

- 根据 `performance_signal_summary.json` 和 `candidate-skill-list.md` 生成 `candidate_skill_list`。
- 使用 `scripts/create_subagent_tasks.py` 生成任务包；默认按采集信息生成候选列表，候选完成后追加 coverage skill，只有人工指定候选时才传 `--subskills`。
- 逐个启动候选分析 subagent；默认串行，避免多个主体争用上下文或环境。生成任务包但未启动 subagent 不算完成。
- 校验 subagent 输出 JSON。
- 使用 `scripts/merge_subagent_results.py` 合并候选池。
- 只在迭代阶段启动执行验证 subagent 执行变更；分析阶段不得让 subagent 修改环境。
- 同一时间只允许一个执行验证 subagent 修改环境。
- 维护 `subagent_invocation_log[]`，每条至少包含 `phase`、`subskill_name`、`task_path`、`subagent_id`、`started_at`、`ended_at`、`status`、`result_path` 和失败原因。最终报告和归档必须能追溯每个 skill 是否真正由 subagent 执行。

若运行平台提供 subagent / Task / multi-agent 工具，主 agent 必须使用该工具启动子任务，并把工具返回的任务 ID 写入 `subagent_invocation_log.subagent_id`。

**Claude Code 平台不可降级**：Claude Code 内置 `Agent` 工具始终可用，因此在该平台上**绝对禁止**使用降级模式，每个候选 skill 和 coverage skill 都必须启动独立 subagent 执行。违反此规则视为合规失败。

只有在下述平台完全不支持任何 subagent 工具时，才允许降级为”显式独立上下文执行”：必须为每个 skill 写出任务包、单独读取对应 `subskills/<name>/SKILL.md`、生成独立结果 JSON，并在日志中标记 `subagent_id=platform_unavailable_degraded_context`。Claude Code、Codex CLI、OpenCode 和 Cursor Agent 均不在此列，不得降级。降级执行不能与主 agent 手写全量候选结果混同。

### 分析 Subagent

- 只负责一个候选 skill。
- 在自己的上下文中读取对应 `subskills/<name>/SKILL.md`。
- 只从 `evidence_snapshot_dir` 读取预采集证据。
- 输出候选动作、风险、验证方法、回退方法和停止条件。
- 不执行正式收益验证，不修改系统。
- 输出中必须包含 `timing.analysis_seconds` 和 `result_path`；主 agent 必须把它折算进 `optimization_timing_details`。

### 执行验证 Subagent

- 只负责当前轮被选中的一个 skill 和一个或一组绑定动作。
- 任务包由 `scripts/create_execution_task.py` 生成。
- 执行前读取候选动作、实施计划、验证计划、回退计划和批准范围。
- 再次校验目标实例身份、资源约束、压测命令和回退条件。
- 在 `agent_action_mode=approved_execute` 且权限满足时实施动作。
- 不得向用户逐个 skill 或逐轮请求批准；只校验执行任务中的 `execution_authorization_scope`。若动作超出授权范围，输出 `blocked_scope_change_required` 并交回主 agent 统一询问。
- 执行复测，记录阶段收益、累计收益、耗时、日志路径和是否回退。
- 若验证失败、收益为负、身份不一致或触发拒绝条件，执行回退并输出 `rejected_optimization_actions`。
- 输出轮次结果到 `rounds/round_N_summary.json`，不得自行进入下一 skill。
- 输出中必须包含 `per_skill_gain_pct` 字段，表示该 skill 独立归因的累计收益（仅在该 skill 的首轮执行时从原始基线计算；后续轮次从上一轮有效配置计算阶段收益）。若因轮次混淆或无法隔离，标记为 `null`。

## 任务包格式

任务包由 `scripts/create_subagent_tasks.py` 生成。必填字段：

- `schema_version`
- `scenario_name`
- `current_run_id`
- `current_run_started_at`
- `current_evidence_status`
- `subskill_name`
- `task_id`
- `target_pid`
- `baseline_path`
- `bottleneck_classification`
- `evidence_snapshot_dir`
- `resource_constraints`
- `workload_hints`
- `candidate_skill`
- `required_output_path`
- `instructions`

兼容字段：`evidence_dir` 和 `dynamic_route` 可继续出现，但新实现必须读取 `evidence_snapshot_dir` 和 `candidate_skill`。
任务包生成器会读取 `evidence_snapshot_dir/snapshot_metadata.json`，若其中 `current_run_id` 与参数不一致、`current_evidence_status != current`、`snapshot_time` 早于 `current_run_started_at`，或 `target_identity` 与本轮目标实例身份不一致，必须拒绝生成任务包。命令行参数不得把快照中的 `stale`、`mixed`、`invalid` 状态覆盖为 `current`。

候选池合并器默认以 `candidate_skill_list` 作为期望 skill 列表；缺少任何候选或 coverage skill 的结果、run_id 不一致、证据状态不为 `current` 或候选动作为空，都会写入 `candidate_pool.json.gate_errors`。执行验证任务生成器必须在存在 `gate_errors` 时拒绝生成任务包。

## 生成任务包示例

```bash
scripts/create_subagent_tasks.py \
  --scenario mysql-readonly \
  --baseline baseline.json \
  --evidence-dir output/evidence \
  --current-run-id mysql-readonly-20260621T100000 \
  --current-run-started-at 2026-06-21T10:00:00+08:00 \
  --current-run-manifest output/current-run-manifest.json \
  --target-identity-path output/target-instance-identity.json \
  --target-pid 12345 \
  --bottleneck cpu_bottleneck \
  --performance-summary output/evidence/performance_signal_summary.json \
  --candidate-reason "hotspot and topdown signals from current evidence" \
  --output-dir output/candidate-skill-tasks \
  --results-dir output/candidate-skill-results
```

## Subagent 输出 JSON

每个 subagent 必须输出以下顶层字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `subskill_name` | string | 是 | 子 skill 名称 |
| `current_run_id` | string | 是 | 本轮运行 ID，必须与任务包一致 |
| `current_evidence_status` | string | 是 | 必须为 `current` 才能输出候选动作 |
| `status` | string | 是 | `ok` / `degraded` / `blocked` / `failed` |
| `confidence` | string | 是 | `high` / `medium` / `low` |
| `analysis_timestamp` | string | 是 | ISO 8601 时间戳 |
| `evidence_sources` | string[] | 是 | 引用证据路径 |
| `findings` | object | 是 | 结构化发现 |
| `candidate_actions` | array | 是 | 候选动作 |
| `required_evidence` | string[] | 是 | 缺失证据；无缺失时为空数组 |
| `fallback_notes` | string[] | 是 | 降级说明；无降级时为空数组 |
| `timing` | object | 是 | 耗时统计 |

## 执行验证输出 JSON

生成执行验证任务包示例：

```bash
scripts/create_execution_task.py \
  --scenario mysql-readonly \
  --round round-1 \
  --subskill application-config-optimization \
  --candidate-pool output/candidate_pool.json \
  --current-run-id mysql-readonly-20260621T100000 \
  --current-run-manifest output/current-run-manifest.json \
  --per-skill-state output/per_skill_iteration_state.json \
  --action-id app-config-001 \
  --baseline output/baseline.json \
  --previous-round output/rounds/baseline_summary.json \
  --evidence-dir output/evidence \
  --output-dir output/execution-tasks \
  --agent-action-mode approved_execute
```

执行验证 subagent 每轮至少输出：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `round` | integer/string | 是 | 当前轮次 |
| `subskill_name` | string | 是 | 当前执行的 skill |
| `current_run_id` | string | 是 | 本轮运行 ID，必须与执行任务一致 |
| `current_evidence_status` | string | 是 | 必须为 `current`，否则输出 `blocked` |
| `action_ids` | string[] | 是 | 本轮执行动作 |
| `execution_status` | string | 是 | `accepted` / `rejected` / `rolled_back` / `blocked` |
| `target_instance_identity` | object | 是 | 执行前后的目标实例校验证据 |
| `before_metrics` | object | 是 | 上一轮有效配置指标 |
| `after_metrics` | object | 是 | 当前轮复测指标 |
| `stage_gain_pct` | number/null | 是 | 相对上一轮收益 |
| `cumulative_gain_pct` | number/null | 是 | 相对初始基线收益 |
| `per_skill_gain_pct` | number/null | 否 | 该 skill 独立归因的累计收益百分比（该 skill 全部轮次相对初始基线的独立贡献）。仅在 `attribution_method=single_variable_round` 时有意义；confounded 轮次为 null |
| `applied_changes` | array | 是 | 已实施动作 |
| `rollback_result` | object | 是 | 回退状态；未回退时说明原因 |
| `logs` | array | 是 | 压测、变更和验证日志路径 |
| `timing` | object | 是 | 分析、实施、验证和总耗时 |
| `subagent_id` | string | 是 | 平台返回的 subagent 任务 ID；平台不支持时写降级上下文 ID |

### candidate_actions 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | string | 唯一标识 |
| `title` | string | 简短标题 |
| `category` | string | 动作分类 |
| `priority` | string | `high` / `medium` / `low` |
| `change_mode` | string | `analysis_only` / `dry_run` / `online` / `restart_required` / `system_reboot` / `rebuild_required` / `hardware_advice` |
| `requires_root` | boolean | 是否需要 root |
| `risk` | string | `low` / `medium` / `high` |
| `implementation_plan` | string | 实施计划 |
| `validation_plan` | string | 验证计划 |
| `rollback` | string | 回退方法 |
| `expected_effect` | string | 预期效果 |
| `expected_gain_metric` | object | 指标与预期收益 |
| `rejection_criteria` | string[] | 不采纳条件 |
| `evidence_refs` | string[] | 证据路径 |

## 候选池合并

```bash
scripts/merge_subagent_results.py \
  --results-dir output/candidate-skill-results \
  --output-candidate-pool output/candidate_pool.json \
  --output-summary output/candidate-skill-summary.md \
  --candidate-manifest output/candidate-skill-tasks/manifest.json \
  --gate-check \
  --expected-subskills application-config-optimization,performance-library-selection,cpu-affinity-optimization \
  --optimization-order application-config-optimization,performance-library-selection,cpu-affinity-optimization
```

`--gate-check` 失败条件：

- 预期 skill 缺失。
- 任一结果 JSON 无效。
- 任一预期 skill 为 `blocked` 或 `failed`。
- `candidate_actions` 为空。

## 推荐文件布局

```text
output/
  checkpoints/
    checkpoint_5.json
    checkpoint_bottleneck.json
    checkpoint_candidate_skill_list.json
  evidence/
    snapshot_metadata.json
    performance_signal_summary.json
  candidate-skill-tasks/
    manifest.json
    <subskill>.json
  candidate-skill-results/
    <subskill>.json
  execution-tasks/
    round-1_<subskill>.json
  rounds/
    round_1_summary.json
  candidate_pool.json
  final-report.md
  review-result.json
  case_archive.json
```
