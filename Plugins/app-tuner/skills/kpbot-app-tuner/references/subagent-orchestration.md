# Subagent Orchestration

候选 skill workflow 使用两类 subagent 执行 `candidate_skill_list` 中的 skill：分析 subagent 和执行验证 subagent。主 agent 只维护全局状态、候选 skill 列表、任务包、候选池、收益统计和最终决策。

## 双阶段强制契约（Dual-Phase Enforcement）

每个候选 skill 和 coverage skill **必须**经历两个独立 subagent 阶段，**禁止合并为单个 subagent**：

1. **分析阶段**（分析 subagent）：独立读 `subskills/<name>/SKILL.md`，按完整流程分析现场证据，输出 `candidate_actions`，**不修改系统**。
2. **执行验证阶段**（执行验证 subagent）：基于分析结果实施、复测、记录收益/回退。

**违规判定**：
- 主 agent 在分析 subagent 的 prompt 中指定具体动作（如 "LD_PRELOAD tcmalloc"、"使用 BiSheng 预编译二进制"），替代 subagent 的独立分析 → 违规。
- 主 agent 跳过分析 subagent，直接启动执行验证 subagent 执行预判动作 → 违规。
- 主 agent 将分析和执行合并为单个 subagent（既分析又执行） → 违规。
- 主 agent 直接手写 coverage skill 结论，未启动 subagent → 违规。

**门控机制**：
- `verify-order`：进入任何其他候选/coverage skill 前，确认 cpu-affinity-optimization 已完成。
- `verify-skill-completeness`：任意 skill 标记 completed 前，校验该 skill 分析 subagent 输出的全部 `candidate_actions` 都有结论（执行/拒绝/阻塞/停止且说明原因），且 `steps_covered` 覆盖 SKILL.md 定义的全部手段类别。`incomplete_skills` 非空时禁止标记 completed。
- `report-ready`：报告生成前合并执行 `validateCompliance + validateReportInputs + verifySkillCompleteness`，任一失败则阻塞。

### executor 强制合规（脚本强制）

`dynamic_workflow_manager.js` 对 subagent 执行记录实施强制校验，文档与脚本保持一致：

- **分析阶段必须为 analyzer**：`append-subagent-log` 的 `phase=analysis` 要求 `subagent_type=analyzer`（统一分析 agent，按任务包 `subskill_name` 字段加载对应 skill；兼容旧值 `oracle`）；传入 executor 会被拒绝。
- **实施阶段必须为 executor**：`phase=implementation` 要求 `subagent_type=executor`，且 `subagent_id` 必须为平台返回的真实 subagent 任务 ID。`platform_unavailable_degraded_context` 在实施阶段被拒绝——OpenCode/Claude Code 均禁止降级。
- **completed/stopped 必须有 executor 记录**：`set-iteration-state` 在状态为 `completed`/`stopped` 且声明轮次时，会校验 `subagent_invocation_log` 中存在对应 skill 的 `phase=implementation` + `subagent_type=executor` 记录；缺失则抛 `EXECUTOR_MANDATORY_VIOLATION`，阻止标记完成。
- **applied 动作必须有证可溯**：`verify-skill-completeness` 发现某个 skill 有已应用动作但无 executor 实施记录时，写入 `incomplete_skills`（`executor_mandatory_violation`），`report-ready` 拒绝通过。
- **主 agent 违规手写结果**：主 agent 自行实施、伪造 before/after 指标或直接写入轮次结果而不启动独立 executor subagent，会因上述校验在 `set-iteration-state` 或 `report-ready` 阶段被拦截。

> **注意**：输入侧的 prompt 约束（如禁止主 agent 在 prompt 中写死动作）本质上是规则而非技术强制，效果有限。根本解决需要架构改进——将 subagent 行为规范从 task prompt 移到 system prompt，由平台加载。详见设计文档第 7 节"架构改进方案：自定义 subagent 类型"。

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

**`subagent_id` 真实性强制规则（ID Authenticity）**：
- 每次 `task`/`Agent` 工具调用返回结果中**都会包含真实任务 ID**（如 OpenCode 的 `ses_<hash>`），主 agent 必须先从工具结果中取出该真实 ID，再调用 `append-subagent-log`（启动时刻 `status=running`，完成时刻更新 `status=completed`）。
- **禁止**使用 subagent 自拟/自报的 ID（例如 `ses_exec_<skill>_r<n>_<date>`、`ses_analyze_...` 等非平台返回格式的 ID）写入日志；禁止用主 agent 自造的簿记号、伪随机串、空串或 `platform_unavailable_degraded_context` 顶替（后者的降级语义仅限平台完全不提供 subagent 工具的降级场景）。
- 无论 executor 在结果 JSON 的 `subagent_id` 字段写入什么值，`subagent_invocation_log` 中的记录都必须以**平台工具返回的真实 task ID** 为准；两者不一致时以平台返回值为准，并在结果校验时提示回写修正。
- `dynamic_workflow_manager.js append-subagent-log` 仅做格式级合法性校验（`_isPlausibleSubagentId`），无法验证 ID 与平台会话的真实绑定；**真实绑定的唯一责任在主 agent**，未从工具结果中取 ID 而手写 ID 视为合规失败。

### 执行验证 Subagent 越权边界（No Overreach）
执行验证 subagent 在同一时刻是唯一的"环境修改主体"，但该权限**严格限定在本轮被指派 skill 的动作**：
- **允许**：实施/复测/回退当前轮动作；向 `execution_log` 追加本轮的 `forward_cmd`/`reverse_cmd`；把本轮结果写入任务包 `required_output_path` 与 `rounds/round_N_summary.json`（含自己的本轮 JSON 产物与归档文件）。
- **禁止**：
  - 修改 `workflow_state.json` 中**其他 skill** 的 `candidate_skill_list[].status` / `coverage_skill_list[].status` / `subagent_invocation_log` / `per_skill_iteration_state` / `per_skill_gain_summary` / `candidate_pool.json` / 其他 skill 的结果 JSON 或任务包。
  - 调用 `update-candidate-status`、`set-iteration-state`、`set-per-skill-gains`、`verify-skill-completeness` 等只应由主 agent 调用的全局门控命令（除 `record-execution` 追加本轮变更外）。
  - 对其他 skill 的分析结论、候选动作、当前 skill 之外的收益做出评估或写入结论。
  - 越权修改一经发现（通过文件时间戳、内容 diff 或后续 `report-ready` 校验定位），该轮收益在 `per_skill_gain_summary` 中标记为 `confounded` 并回滚被篡改字段。

### 互斥/替代候选方案验证（Mutually-Exclusive Full Validation）
当同一 skill 的分析输出包含**互斥或相互替代**的候选方案（针对同一优化目标、不能同时保留），主 agent 必须：
- 在 `candidate_actions` 中识别 `mutually_exclusive_with` 组，为组内**每个方案逐一创建独立验证轮次**（仍遵守单变量原则，每轮只变更一个方案）。
- **全部互斥方案都完成 A/B 验证**后才能裁决：比较相对同一基准的 `after_metrics`/`stage_gain_pct`，选收益最优者保留（该 skill 唯一有效配置，`accepted`），其余全部 `rejected` 并执行 `reverse_cmd` 回退，写入 `rejected_optimization_actions` 并附各方案对比表。
- **禁止**在第一个方案获得正收益后就以"收益已为正、单变量原则、节省轮次"为由跳过其余互斥方案；若确因外部限制（如无充分验证窗口）跳过，必须把跳过方案标记为 `blocked`/`deferred` 并说明，`verify-skill-completeness` 将该 skill 判为 `incomplete` 直至补齐。

**主流平台不可降级**：Claude Code 内置 `Agent` 工具、OpenCode 提供 `task` 工具，均始终可用，因此在这两个平台上**绝对禁止**使用降级模式，每个候选 skill 和 coverage skill 都必须启动独立 subagent 执行。违反此规则视为合规失败。

只有在下述平台完全不支持任何 subagent 工具时，才允许降级为”显式独立上下文执行”：必须为每个 skill 写出任务包、单独读取对应 `subskills/<name>/SKILL.md`、生成独立结果 JSON，并在日志中标记 `subagent_id=platform_unavailable_degraded_context`。Claude Code、Codex CLI、OpenCode 和 Cursor Agent 均不在此列，不得降级。降级执行不能与主 agent 手写全量候选结果混同。

### 分析 Subagent

- 只负责一个候选 skill。
- **必须独立执行 subskill SKILL.md 的完整流程**：读取对应 `subskills/<name>/SKILL.md` 后，按其定义的完整流程独立执行，不得跳过任何步骤。
- **任务包中的路径、配置、优化参数仅作为背景参考**，不得作为执行指令直接照搬。subagent 必须基于自己采集的现场证据独立判断：
  - 哪些候选库/参数/动作应该推荐（依据 subskill SKILL.md 的证据分级与阈值规则）
  - 哪些动作应拒绝（依据现场证据不满足条件）
  - 哪些 subskill 要求的采集（如 perf sampling、库检测）需要现场执行而非依赖预采集
- 从 `evidence_snapshot_dir` 读取预采集证据作为起点；**当 subskill SKILL.md 要求采集的证据在预采集快照中缺失或不完整时，subagent 必须现场补充采集**（如 perf record、lsof、detect_all_libraries.sh），不得以"预采集未覆盖"为由跳过 subskill 的 Step 1。
- **禁止以任务包 instructions 中的预设路径替代 subskill 流程**：例如任务包写明"S5: tcmalloc LD_PRELOAD"，subagent 仍必须执行 performance-library-selection 的完整流程（热点采集 → 库类型识别+规则匹配 → 验证流程设计），独立判断应推荐哪些库（可能包括 tcmalloc 之外的其他库如 stringlib）。
- 输出候选动作、风险、验证方法、回退方法和停止条件。
- **互斥关系声明**：若存在针对同一目标、不能同时保留的多个候选动作（如不同 allocator、同一动作的开关 vs 降级档、不同绑核策略），必须通过 `candidate_actions[].mutually_exclusive_with` 字段显式声明互斥组（值为同组其他 action_id 数组），并在 `findings` 中说明组内预期收益排序与选择标准；未声明互斥关系时，主 agent 默认按独立可叠加动作处理。
- 不执行正式收益验证，不修改系统。
- 输出中必须包含 `timing.analysis_seconds` 和 `result_path`；主 agent 必须把它折算进 `optimization_timing_details`。
- 输出中必须包含 `independent_analysis_confirmation` 字段，声明"本分析已独立执行 subskill SKILL.md 完整流程，任务包背景信息未替代现场证据采集和独立判断"。

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
- **每步记录 forward_cmd / reverse_cmd**：实施动作前，必须先记录 `forward_cmd`（即将执行的变更命令）和 `reverse_cmd`（可独立执行的回退命令），通过 `record-execution` 写入 `workflow_state.json`。`reverse_cmd` 必须可独立执行、不依赖对话上下文、包含完整的环境变量和路径。未记录 forward_cmd/reverse_cmd 的轮次不得标记为已完成。
- **归档原始数据**：每轮必须归档以下原始数据到 `rounds/round_N_<skill_name>/` 目录：
  - `benchmark_raw.csv` — 压测原始 CSV 输出（含 throughput/TTFT/TPOT 全字段）
  - `env_before.json` — 变更前环境变量和进程状态快照
  - `env_after.json` — 变更后环境变量和进程状态快照
  - `forward_cmd.txt` + `reverse_cmd.txt` — 本轮变更和回退命令
  - `server_log.txt` — 服务启动/运行日志片段
  - `npu_metrics.json`（AI 推理场景）— NPU 利用率/HBM 带宽采样
  - `perf_data.bin`（如本轮有采集）— perf record 原始数据
  - 归档文件不得脱敏或截断；脱敏仅在案例归档（`case_archive.json`）阶段执行。
  - 原始数据归档路径写入 `round_N_summary.json` 的 `raw_data_archive_dir` 字段。

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
- `experience_hints`

兼容字段：`evidence_dir` 和 `dynamic_route` 可继续出现，但新实现必须读取 `evidence_snapshot_dir` 和 `candidate_skill`。
任务包生成器会读取 `evidence_snapshot_dir/snapshot_metadata.json`，若其中 `current_run_id` 与参数不一致、`current_evidence_status != current`、`snapshot_time` 早于 `current_run_started_at`，或 `target_identity` 与本轮目标实例身份不一致，必须拒绝生成任务包。命令行参数不得把快照中的 `stale`、`mixed`、`invalid` 状态覆盖为 `current`。

**任务包信息分层**：

| 层 | 字段 | 内容 | subagent 使用方式 |
|----|------|------|------------------|
| 现场证据 | `evidence_snapshot_dir` + `performance_signal_summary_path` + `baseline_path` | perf record 原始数据、DSO 排名、热点函数、topdown、基线指标 | subagent 直接读文件，数据完整可验证 `current_run_id`。**这是分析的唯一数据源** |
| 经验参考 | `experience_hints` | 历史收益数据、推荐路径线索、场景适配性经验 | 仅供参考，不是执行依据。subagent 在 `findings` 中标注命中的经验库技术名，但推荐决策必须基于现场证据 |
| 执行规则 | `instructions` | "只读不修改"、"输出 JSON 格式"、"覆盖 SKILL.md 全部步骤"、"输出 steps_covered 和 independent_analysis_confirmation" | 纯规则，不含数据，不含经验 |

**`experience_hints` 字段约束**：
- 内容来自子 SKILL 的 `references/` 经验库（如 `ascend-playbook.md`、`optimization_kb.json`），由 `create_subagent_tasks.py` 自动提取，不由主 agent 手写
- 每条经验必须标注 `source`（来源文件），subagent 可追溯
- subagent 输出时在 `findings` 中标注命中的经验库技术名，但推荐决策（`recommendation`/`confidence`）必须基于现场证据分级

> **注意**：`instructions` 字段约束是规则性的，主 agent 仍可能在 task prompt 中写入具体动作指令替代 subagent 的独立分析。这是当前架构的固有局限，根本解决需要自定义 subagent 类型（见设计文档第 7 节）。

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
| `independent_analysis_confirmation` | string | 是 | 声明"本分析已独立执行 subskill SKILL.md 完整流程，任务包背景信息未替代现场证据采集和独立判断" |
| `steps_covered` | array | 是 | 声明覆盖的 SKILL.md 步骤和手段类别（如 performance-library-selection 的 18 类库名）。`verify-skill-completeness` 校验是否覆盖全部必填类别 |

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
