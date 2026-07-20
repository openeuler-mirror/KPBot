---
name: kpbot-app-tuner
description: 使用该 skill 构建和执行服务器应用优化 Agent 的自顶向下优化流程，覆盖用户场景输入、Agent 可选操作确认、基线数据确认、环境诊断与备份、磁盘/网卡/内存/CPU/GPU/NPU/硬件规格瓶颈识别、基线后按进程火焰图/热点函数/热点 so/topdown L1 icache miss/L3 cache miss/线程切换等性能信息采集、根据采集信息生成候选优化 skill 列表、按候选列表依次执行 CPU 亲和性、网络参数、性能库、应用配置、BIOS、OS、编译和 Other 等优化 skill、候选完成后覆盖执行未命中 skill、分轮收益统计、review 与环境还原、案例归档和最终报告输出。适用于 Claude Code、Codex、Cursor、OpenCode 以及其他支持目录式 SKILL.md 的编程 Agent 环境。
---

# Server Application Optimization

## Overview

该 skill 是“服务器应用优化 Agent”的主编排入口。主流程必须严格按架构图自顶向下执行：先完成用户界面层确认，再完成环境诊断与备份，再进行性能采集和瓶颈识别，随后根据采集信息生成候选优化 skill 列表并依次执行，最后输出报告、review、环境还原并归档为案例。

当前仓库已经沉淀了服务器应用优化能力；本次架构以服务器应用优化为上层框架，CPU 亲和性、网络、性能库、应用配置、BIOS、OS、编译等能力均作为候选优化 skill 列表中的可选 skill 执行。

## Startup Banner

本 skill 被触发时，第一步必须执行以下命令，再开始任何其他工作流程：

```bash
SKILL_NAME=kpbot-app-tuner; bash .opencode/skills/$SKILL_NAME/scripts/print_logo.sh 2>/dev/null || bash "$HOME/.config/opencode/skills/$SKILL_NAME/scripts/print_logo.sh" 2>/dev/null || bash .claude/skills/$SKILL_NAME/scripts/print_logo.sh 2>/dev/null || bash "$HOME/.claude/skills/$SKILL_NAME/scripts/print_logo.sh"
```

该 banner 只在全流程首次启动时打印一次，后续阶段不再重复。

## Dynamic Workflows Runtime

本 skill 在 Claude Code 中按 Dynamic Workflows 运行：主 agent 只负责门控、状态、路由、候选池、收益统计和报告；具体优化分支只有在证据命中后才加载和执行。

运行时必须维护 `current_workflow_state`：

```json
{
  "current_gate": "bootstrap",
  "completed_gates": [],
  "blocked_gate": null,
  "next_gate": "scenario-intake",
  "current_run_id": null,
  "evidence_status": "missing",
  "candidate_skill_list": [],
  "coverage_skill_list": [],
  "active_workflow": null,
  "workflow_trace": []
}
```

Dynamic Workflows 由四类 workflow 组成：

| Workflow 类型 | 触发条件 | 输出 |
|---|---|---|
| Gate workflow | 上游确认、备份、基线、证据尚未完成 | gate 状态、阻塞原因、下一步 |
| Candidate routing workflow | 已有当前基线和深度性能证据 | `candidate_skill_list` |
| Candidate skill workflow | `candidate_skill_list` 命中对应 subskill | 候选动作、风险、验证、回退、轮次收益 |
| Coverage skill workflow | 候选 workflow 完成后仍有主优化 subskill 未形成结论 | coverage 分析、阻塞原因或验证结果 |
| Closeout workflow | 无活动瓶颈、全部 skill 停止或流程阻塞 | 报告、review、还原计划、案例归档 |

执行原则：

- 启动时不得批量读取所有 subskill；只读取 Phase 1 强制 references。
- 每次进入、跳过、阻塞、回流或停止 workflow，都必须追加 `workflow_trace`。
- `candidate_skill_list` 是候选阶段的 subskill 加载依据；未命中的主优化 subskill 只在 coverage 阶段加载并形成结论。
- **Claude Code 平台必须使用 `Agent` 工具启动独立 subagent 执行每个候选 skill 和 coverage skill**，禁止主 agent 自己读取 subskill 并手写分析结果。仅在平台完全不提供 subagent 工具时允许降级，Claude Code 不在此列。详见 `references/subagent-orchestration.md`。
- 单个 candidate 或 coverage workflow 连续 5 轮收益均小于 1% 时停止该 workflow；若仍有候选或 coverage 项，继续下一个 workflow。
- 当前证据缺失、过期或与 `current_run_id` 不一致时，只能输出 `blocked` 或 `degraded`。

### Dynamic Workflow State 持久化

`current_workflow_state` 必须通过 `scripts/dynamic_workflow_manager.js` 持久化到磁盘。每个关键节点调用对应 CLI 命令：

| 时机 | CLI 命令 | 说明 |
|------|----------|------|
| 启动门控完成 | `init --run-id <id> --output-dir <dir>` | 创建初始 state，写入 workflow_state.json |
| 进入每个新门控 | `gate-enter --state <path> --gate <name>` | 更新 current_gate / next_gate |
| 完成每个门控 | `gate-complete --state <path> --gate <name> [--evidence <path>]` | 追加到 completed_gates |
| 门控阻塞 | `gate-block --state <path> --gate <name> --reason <text>` | 设置 blocked_gate，记录阻塞原因 |
| 阻塞解除 | `gate-unblock --state <path>` | 清除 blocked_gate |
| 证据采集完成 | `set-evidence --state <path> --status current` | 更新 evidence_status |
| 候选列表生成 | `set-candidates --state <path> --candidates '<json>'` | 自动预置 cpu-affinity 到首位 |
| coverage 生成 | `auto-coverage --state <path>` | 自动补充未命中 coverage skill |
| **候选 skill 完成/阻塞** | **`update-candidate-status --state <path> --subskill <name> --status <pending\|running\|completed\|stopped\|blocked> [--result <json>]`** | **每个 skill 完成时必须调用，否则 validate 失败** |
| **每轮动作完成** | **`set-iteration-state --state <path> --data '<json>'`** | **写入 per_skill_iteration_state，记录轮次收益和停止原因** |
| **每轮动作完成** | **`set-timing --state <path> --data '<json>'`** | **写入 agent_timing_summary + optimization_timing + optimization_timing_details** |
| **每个 subagent 调用** | **`append-subagent-log --state <path> --data '<json>'`** | **记录 subagent 调用，缺失则禁止生成完成态报告** |
| **所有 skill 完成** | **`set-per-skill-gains --state <path> --data '<json>'`** | **写入 per_skill_gain_summary** |
| **每次真实变更执行后** | **`record-execution --state <path> --data '<json>'`** | **记录 forward_cmd + reverse_cmd，用于环境恢复** |
| 追加 trace 条目 | `trace --state <path> --gate <name> --event <name>` | 记录跳过、回流等事件 |
| **报告生成前** | **`report-ready --state <path>`** | **合并硬门控：同时执行 validate + validate-report-inputs。任一项失败则输出 issues + remediation 并 exit≠0，绝对禁止继续生成完成态报告** |
| 报告生成时 | `summary --state <path>` | 输出合规摘要 + workflow_trace（**内部自动调用 report-ready，未就绪则阻塞并 exit≠0**） |
| **环境恢复确认** | **`restore-plan --state <path>`** | **生成恢复计划（LIFO 逆序），供用户确认后执行** |
| **恢复步骤完成** | **`mark-step-reverted --state <path> --index <n>`** | **标记单个恢复步骤已完成** |

### 强制持久化检查点

以下节点**必须**调用对应 CLI 命令写入 workflow_state.json，不得只在对话上下文中记录：

1. **每个 candidate skill 执行完毕后** → `update-candidate-status` + `set-iteration-state` + `set-timing`
2. **每次启动 subagent 前后** → `append-subagent-log`（启动时 status=running，完成时 status=completed）
3. **每次执行真实变更时** → `record-execution`，必须同时写入 `forward_cmd` 和 `reverse_cmd`。`reverse_cmd` 必须可独立执行且不依赖上下文
4. **所有 skill 迭代完成后、报告生成前** → `set-per-skill-gains` → `report-ready`（合并硬门控，替代单独的 validate + validate-report-inputs）
5. **若 report-ready 返回 `ready: false`** → 根据输出的 `issues` 和 `remediation` 补齐缺失数据，重新校验直到 `ready: true`。**绝对禁止跳过此门控直接生成完成态报告**。`summary` 命令内部也会自动调用 `report-ready`，未就绪时拒绝输出
6. **报告完成后** → 展示恢复计划，**使用 `AskUserQuestion` 询问用户是否恢复环境**，未获确认前不得跳过

### 🕐 每轮耗时实时记录（Anti-事后补数据）

**每轮动作（分析→实施→验证→回退判断）完成后，必须在验证结果输出后 30 秒内调用以下命令**，不得累积到报告阶段补写：

```bash
# 1. 记录轮次状态与收益
node scripts/dynamic_workflow_manager.js set-iteration-state \
  --state <workflow_state.json> \
  --data '{"subskill":"<name>","round":<n>,"gain_tps_pct":<x.x>,"gain_p95_pct":<x.x>,"status":"<completed|stopped>"}'

# 2. 追加单条 optimization_timing_details 记录（必须包含本轮实际秒数）
node scripts/dynamic_workflow_manager.js set-timing \
  --state <workflow_state.json> \
  --data '{"optimization_timing_details":[{"stage":"candidate-skill-iteration","skill_name":"<name>","round_name":"round-<n>","status":"completed","analysis_seconds":<n>,"implementation_seconds":<n>,"validation_seconds":<n>,"total_seconds":<n>}]}'

# 3. 若执行了真实变更
node scripts/dynamic_workflow_manager.js record-execution \
  --state <workflow_state.json> \
  --data '{"step":<n>,"skill":"<name>","forward_cmd":"<cmd>","reverse_cmd":"<cmd>"}'
```

**违规判定**：若 `report-ready` 阶段发现 `optimization_timing_details` 条目数少于实际执行轮次数，或 `set-timing` 调用时间晚于验证完成 >5 分钟，报告必须标记 `timing_recorded_retroactively=true` 并降级为 `degraded`。

## Three-Phase Refactor Contract

本 skill 的实现和后续迭代分三阶段维护：

1. **Phase 1 - 顶层 Agent 架构与用户界面契约**
   - 建立测试场景输入、Agent 可选操作确认、基线数据确认、数据统计输出。
   - 执行环境诊断与环境备份，形成可回退基线。
   - 读取 `references/application-agent-architecture.md`、`references/input-contract.md`、`references/user-interaction-gates.md`、`references/checklist.md`。
2. **Phase 2 - 性能信息采集与候选优化 skill 列表生成**
   - 使用性能采集工具识别磁盘、网卡、内存、CPU、GPU/NPU、硬件规格瓶颈。
   - 基线确认后抓取按进程火焰图、热点函数、热点 DSO/so、进程/线程、topdown L1 icache miss、L3/LLC cache miss 和上下文切换证据。
   - 根据采集信息生成 `candidate_skill_list`，详见 `references/candidate-skill-list.md`。
3. **Phase 3 - 迭代优化、review、还原与案例归档**
   - 每个 skill 独立跟踪轮次收益；同一 skill 连续 5 轮收益均小于 1% 时停止该 skill。
   - 先执行证据命中的候选 skill；候选完成后覆盖执行未进入候选列表的主优化 skill，直到所有主优化 skill 都有结论后输出报告。
   - 执行 review、环境还原和案例归档，详见 `references/review-restore-archive.md`。

## Architecture-Aligned Dynamic Workflow

按以下顺序执行，不得跳过上游确认直接进入下游优化：

0. **启动门控**
   - 读取 `references/application-agent-architecture.md`、`references/input-contract.md`、`references/user-interaction-gates.md`、`references/checklist.md`。
   - 创建 TodoWrite 检查清单，覆盖用户界面、环境备份、瓶颈识别、候选 skill 列表、优化轮次、报告、review、还原和归档。
   - 初始化并持续更新 `overall_progress`，至少包含 `current_gate`、`completed_gates`、`blocked_gate`、`next_gate` 和 `status`。每进入或阻塞一个门控，都必须向用户展示当前总体进度。
   - 初始化并持续更新 `current_workflow_state`；没有该状态时不得进入候选 skill 或 coverage skill workflow。
   - 初始化 `workflow_execution_plan`、`workflow_stage_trace` 和 `timing_jsonl_path`。每个门控进入时记录 `started_at`，离开时记录 `ended_at`、`duration_seconds`、`status` 和证据路径；不得只在最终报告里补写流程。
   - 每次进入新门控时，必须向用户展示当前 workflow 阶段，例如 `Phase 1/3 - environment_diagnosis_confirmation`、已完成阶段和下一阶段。
1. **用户界面层确认**
   - 按 `references/user-interaction-gates.md` 主动询问用户是否已提供当前应用场景信息和环境信息；缺失项必须形成待补充清单，不能默默进入下游。
   - 收集测试场景输入：应用、workload、部署组网、压测方法、目标规格、允许变更范围。
   - 即使用户提供的是完整测试报告，也必须先抽取并概括 `scenario_environment_summary`，至少包含场景、环境、客户端/服务端节点、容器/虚拟化、压测命令、目标规格、已发现历史结果和缺失项，并要求用户显式确认。`scenario_confirmation_status != confirmed` 时不得进入环境备份。
   - 询问并记录 `optimization_entry_mode`：`baseline_first`、`running_app` 或 `historical_baseline_review`。提问中必须逐项注释三种模式的含义、适用场景和后续门控，再按用户选择进入对应路线。
   - 输出 Agent 可选操作清单：只读分析、dry-run、允许在线变更、允许重启、允许重编译、允许硬件调整建议。涉及 `agent_action_mode` 或其他模式选择时，提问中必须说明每个模式会允许/禁止哪些动作。
   - 场景和操作边界确认后生成本轮唯一 `current_run_id` 和 `current_run_manifest`，所有基线、证据、任务包、候选池、轮次结果和报告都必须引用该 ID。没有本轮 ID 的产物只能作为历史记录，不能作为当前结论。
   - 基线测试后必须让用户确认基线数据，确认前不得进入候选优化 skill 列表生成。
   - 建立数据统计口径：Agent 调优耗时统计、各手段提升比例、阶段收益与累计收益。
2. **环境诊断 + 环境备份**
   - 先识别 `node_inventory`。涉及多台机器时，必须按节点角色逐台处理，例如 `benchmark_client`、`application_server`、`database_server`、`load_balancer`。每台机器都必须使用相同的环境备份和诊断契约，不能只采服务端。
   - 执行环境备份前必须逐个物理节点询问是否采集 BIOS 配置；用户选择采集时，再询问该节点的 BMC/IPMI/Redfish 地址、账号、密码/token 或环境变量传递方式；用户不采集时也必须得到显式 `skip` 确认并记录为跳过。未询问或未确认时，停止在 `bmc_redfish_confirmation`。
   - 运行或指导运行 `scripts/backup_environment.sh <output-dir>`。多节点场景必须为每个节点生成独立 `environment_backup_dir`，并在 `per_node_environment_backups` 中汇总。
   - 记录 CPU/NUMA、内存、磁盘、网卡、GPU/NPU、BIOS、OS、Kernel、容器/虚拟化、编译器、运行时、应用配置。
   - 容器场景必须同时采集宿主机视角和容器内视角。宿主机采集用于硬件、内核、PMU、网卡和 IRQ；容器内采集用于应用配置、进程、文件路径、cgroup、用户态依赖和数据库状态。只要容器可进入，应用操作和配置读取优先在容器内执行；不能进入时必须记录 `container_access_status=blocked|degraded` 和最小修复建议。
   - 备份后必须按 `references/environment-diagnosis.md` 执行环境诊断，至少检查：历史 reference 问题集回归、BIOS 是否高性能配置、perf/PMU 采集能力是否可用、内核补丁是否齐全。
   - 多节点场景必须输出 `per_node_environment_diagnosis`。服务端和客户端都要诊断工具依赖、OS/Kernel、网络、CPU/NUMA、容器/虚拟化和权限；服务端额外诊断应用/数据库实例，客户端额外诊断压测工具和客户端瓶颈风险。
   - 历史 reference 问题集或内核补丁清单不存在时，对应诊断项标记为 `skipped` / `not_present`，不得报错；存在时必须逐项检查并输出证据。
   - 若 perf、PMU 事件、root/capability、容器/虚拟机映射或系统命令权限不足，必须提前告知用户受影响的采集能力和修复建议。
   - 环境诊断完成后必须向用户展示诊断摘要、阻塞项、降级项、证据路径和下一步建议，并记录 `environment_diagnosis_confirmation_status`。
   - 用户显式确认诊断结果前，不得进入服务健康检查、目标实例身份确认、正式基线或基线性能确认；用户要求修复或补采时，必须停在 `environment_diagnosis_confirmation` 门控。
   - 生成 `environment_backup_dir` 和 `restore_baseline_manifest`，供 Phase 3 review 与还原使用。
3. **服务健康检查与目标实例身份确认**
   - 进入本步骤前必须满足 `environment_diagnosis_confirmation_status=confirmed`。
   - 在正式基线前必须检查目标服务是否可用于压测：端口监听、协议级连接、认证/权限、最小查询或请求、1 线程/最小负载 smoke test。
   - 必须记录 `service_health_status`、`service_health_evidence` 和 `target_instance_identity`。服务未启动、端口不通、认证失败、实例身份不明或 smoke test 失败时，必须停止在该门控，向用户报告具体问题和修复建议。
   - 服务健康检查未通过时，不得运行正式基线、不得进入瓶颈识别、不得基于历史报告直接交付优化结论。
4. **基线建立与确认**
   - 在目标规格约束下建立基线；若用户给出 `8U32G` 等规格，必须先约束资源再测试。
   - 记录测试组网、测试用例信心、压测命令、原始日志、目标实例身份和基线指标。
   - **基线运行期间只能采集资源级指标**：CPU %util（mpstat/pidstat）、内存 RSS、磁盘 IOPS（iostat）、网络带宽（sar -n DEV）。**不得采集微架构级证据**：perf stat -e（cycles/instructions/cache-misses 等）、perf record（火焰图数据）、topdown 指标均属于深度证据，必须在用户显式确认基线后才进入 Step 6 采集。
   - 基线测试完成后必须展示基线指标、资源利用率、压测命令、用例信息、目标规格和目标实例身份，让用户确认该基线与用例可作为本轮调优依据。
   - 用户显式确认基线后写入 `checkpoint_5.json`。如果用户未回复或确认不明确，`baseline_confirmation_status` 必须保持 `pending`，不得进入瓶颈识别、深度证据采集、候选列表或任何优化动作。
   - 发现历史日志、历史报告或旧轮次结果时，只能记录为 `historical_records_status=discovered_unconfirmed`；是否作为本轮输入必须先由用户确认。未经确认不得用历史记录替代现场基线或触发进一步调优。
5. **性能采集与瓶颈识别**
   - 使用 `scripts/detect_bottleneck.sh` 和相关采集命令识别磁盘、网卡、内存、CPU、GPU/NPU、硬件规格瓶颈。
   - 若仍有瓶颈且未达到停止条件，继续进入性能信息采集和候选列表生成；若无法识别瓶颈或所有优化完成，进入报告阶段。
6. **基线后深度证据采集**
   - 运行或指导运行 `scripts/collect_evidence_snapshot.sh`，必须传入 `--current-run-id`、`--current-run-started-at`；若已生成目标实例身份 JSON，同时传入 `--target-identity-path`。
   - 必须尽量采集按进程火焰图或 perf data、热点函数、热点 DSO/so 排序、进程/线程分布、topdown L1 icache miss、L3/LLC cache miss、上下文切换、FE bound 等证据。
   - `scripts/collect_evidence_snapshot.sh` 应生成 `performance_signal_summary.json`，至少包含 `hotspot_function_rank`、`hotspot_dso_rank`、`topdown`、`threading` 和 `detected_signals`。
   - 证据不足时，候选列表生成只能输出 `blocked` 或 `degraded`，不得把猜测当结论。
   - 若发现证据缺失、过期、run_id 不一致、目标实例不一致或混入历史产物，必须设置 `current_evidence_status=stale|mixed|invalid` 并停止在当前门控，优先输出异常状态。
7. **候选优化 skill 列表生成**
   - 读取 `references/candidate-skill-list.md`。
   - 根据采集信息生成 `candidate_skill_list`，而不是只按静态瓶颈表选择 skill。
   - **`cpu-affinity-optimization` 始终作为第一优先级候选 skill**：无论采集信号是否命中，`cpu-affinity-optimization` 必须始终位于 `candidate_skill_list` 首位（`priority=highest`），在所有其他候选 skill 之前执行。它是唯一不受信号阈值约束的强制候选 skill。
   - 热点 DSO/so 中存在高占比第三方库时，把 `performance-library-selection` 加入候选列表。
   - 网络相关热点函数占比高时，把 `network-optimization` 加入候选列表。
   - topdown 或 perf stat 显示 L1 icache miss 高时，把 `compiler-optimization` 加入候选列表，重点分析 PGO/LTO。
   - 线程切换高且 L3/LLC cache miss 高时，把 `cpu-affinity-optimization` 加入候选列表（当证据触发时，理由为证据命中；未触发时，理由为强制基线检查）。
   - 候选列表完成后，必须追加未命中的主优化 skill 作为 coverage 阶段，确保最终所有主优化 skill 都有分析、执行验证或阻塞结论。
   - 使用 `scripts/create_subagent_tasks.py` 生成任务包后，主 agent 必须按 `references/subagent-orchestration.md` 为 `candidate_skill_list` 中每个 skill 启动独立分析 subagent；禁止由主 agent 直接代替所有子 skill 分析并手写结果。
8. **Skill 迭代优化**
   - 每个候选 skill 先由独立分析 subagent 产出候选动作、实施计划、验证方法、风险和回退方法。
   - **单变量原则（Single-Variable Principle）**：每个执行轮次只能变更来自一个候选 skill 的变量。即使多个 skill 的变更都只需要同一次重启（如 MySQL 重启），也必须拆分为独立轮次，每轮仅变更一个 skill 的变量，并在每轮间执行基准验证以隔离收益归因。禁止将多个 skill 的变更合并到同一轮次中执行。当因实际限制必须合并执行时，结果必须标记为 `confounded`。
   - **cpu-affinity-optimization 执行顺序门控**：进入任何其他候选 skill 的执行轮次前，必须确认 `cpu-affinity-optimization` 已完成（状态为 `completed` 或 `stopped`）。主 agent 必须在启动其他 skill 前调用 `DynamicWorkflowManager.validateCpuAffinityFirst()` 确认通过。违规执行视为合规失败。
   - Claude Code 按 Dynamic Workflows 调度候选 skill：优先执行 `candidate_skill_list` 命中的 workflow，再进入未命中主优化 skill 的 coverage workflow；未进入当前候选或 coverage 阶段的 subskill 不得提前加载或执行。
   - 基线确认后，不得对每个 skill 或每轮动作逐个询问用户是否继续。主 agent 只校验已确认的 `agent_action_mode`、`change_scope`、重启/重编译/远程执行/硬件建议等授权边界；若候选动作都在已确认边界内，直接串行下发执行验证 subagent。只有出现超出已确认边界的新动作、风险等级升级、需要新增权限、需要重启/重编译/硬件调整但未授权，或用户显式要求人工确认时，才返回用户确认门控。
   - 真实危险动作仍必须处于 `approved_execute` 且有回退计划；但该批准应来自启动阶段或基线确认后的批量执行授权，不得退化为每个子 skill 的重复确认。
   - 每个 skill、每轮候选动作和每次验证都必须记录 `analysis_seconds`、`implementation_seconds`、`validation_seconds`、`total_seconds`，并写入 `optimization_timing`、`optimization_timing_details` 和 `timing_jsonl_path`。推荐使用 `scripts/record_timing.py`，也可生成等价 JSONL。
   - **单 skill 收益归因要求**：每个 skill 完成后必须记录其独立可量化的收益归因，写入 `per_skill_gain_summary`。归因必须包含 skill 名称、执行轮次、各轮阶段收益、累计收益、归因方法（`single_variable_round` / `baseline_reset` / `merged_unresolvable` / `confounded`）、证据路径和停止原因。合并轮次或无法隔离的收益必须标记为 `confounded` 并说明不可归因的原因。
   - 每轮必须启动一个执行验证 subagent 串行负责当前 skill 的实施、复测、回退和结果记录；同一时间只允许一个执行主体修改环境。未产生 subagent 任务包、subagent ID 或结果 JSON 的 skill 不能标记为完成。
   - 主 agent 只维护全局状态、候选池、收益统计和继续/停止决策，不并发修改环境。
   - 单个 skill 最多尝试 5 轮；若 5 轮收益均小于 1%，该 skill 停止并继续下一个候选或 coverage skill。
   - 所有主优化 skill 都完成、停止或阻塞并说明原因后，才能进入最终报告。
9. **报告输出** — **必须使用 `scripts/generate_report.py` 生成，禁止手写 markdown**

	   1. 从 `workflow_state.json` 和本轮产物中汇总 `report-input.json`。
	   2. **必须通过** `scripts/generate_report.py --input report-input.json --output <final_report.md>` 生成报告。
	      ```bash
	      python3 scripts/generate_report.py --input <report-input.json> --output <final_report.md>
	      ```
	   3. 脚本内置**完整模板**（52 个强制字段 + 所有章节），永远不会遗漏耗时统计、贡献分解、Gate 轨迹、回退计划等章节。手写 markdown 无此保证。
	   4. 若脚本输出 `Missing architecture fields` → 补全 `report-input.json` 对应字段后重新生成。
	   5. **报告生成前必须通过硬门控 `report-ready`**：
	     ```bash
	     node scripts/dynamic_workflow_manager.js report-ready --state <workflow_state.json>
	     ```
	     该命令合并执行原 `validate`（11 项合规自检）+ `validate-report-inputs`（5 项必填字段检查）。若 `ready: false`，根据输出的 `issues` 和 `remediation` 补齐缺失数据，重新执行直到通过。**绝对禁止跳过此门控**。
	   - 运行 `node scripts/dynamic_workflow_manager.js summary --state <workflow_state.json>` 获取合规摘要和 `workflow_trace`，一并写入报告。**`summary` 内部自动调用 `report-ready`，未就绪时拒绝输出（exit≠0）**。
	   - **报告写完后必须自查以下 5 个高频遗漏章节是否已在 markdown 中出现**（不依赖用户提醒）：

	     | # | 章节 | 关键词 grep | 数据源 |
	     |---|------|-------------|--------|
	     | 1 | **Agent 耗时统计** | `耗时统计` 或 `timing` | `workflow_state.json` → `agent_timing_summary` + `optimization_timing` + `optimization_timing_details` |
	     | 2 | **各 Skill 贡献分解** | `贡献分解` 或 `per_skill` | `workflow_state.json` → `per_skill_gain_summary` |
	     | 3 | **Workflow Gate 执行轨迹** | `Gate` 和 `completed` | `summary` 输出 → `workflow_trace` |
	     | 4 | **恢复/回退计划** | `回退` 或 `reverse` 或 `restore` | `execution_log` → 每条 `forward_cmd` + `reverse_cmd` |
	     | 5 | **环境诊断摘要** | `环境诊断` | `environment_diagnosis.json` |

	     **自查方法**：报告写出后，用上述关键词 grep 报告文件。缺失任一章 → 补齐后再提交给用户。禁止把"等用户提醒再补"当作正常流程。
   - 最终报告必须包含环境信息与软硬件信息、测试组网与测试用例信心、`workflow_execution_plan`、`workflow_stage_trace`、`workflow_trace`、Dynamic Workflows 合规摘要、调优报告 workflow 流程分析、瓶颈分析、优化手段验证效果和耗时、结论与下一步计划。
   - `overall_progress.status=completed` 的报告必须包含非空 `agent_timing_summary`、`optimization_timing` 和 `optimization_timing_details`。缺失任一耗时字段时不得生成完成态报告，必须停在 `report_input_validation` 并补齐计时记录。
   - 若流程阻塞在服务健康、实例身份、基线确认或权限门控，报告必须明确 `overall_progress`、`blocked_gate`、失败证据和下一步修复建议；不得把历史报告或旧日志包装成最终优化结果。
   - 若 `current_evidence_status != current` 或 `current_run_id` 缺失，报告必须转为阻塞/降级报告，说明当前异常和需要补采的最小证据；不得输出正式收益、候选动作或优化结论。
   - 报告 schema 见 `references/report-schema.md`，生成规则见 `references/report-generation.md`。
10. **Review、环境还原与案例归档**
   - 执行 review，确认最终有效配置、已回退配置、残留风险和不可实施动作。
   - 按 `restore_baseline_manifest` 执行或输出环境还原计划。
   - 归档 `case_archive.json`，后续作为案例迭代输入。

## Dynamic Skill Catalog

主编排只负责候选 skill 列表生成、门控、候选池、收益统计和报告，不把所有专项逻辑写进主上下文。

| 架构图 skill | 当前目录实现 |
|---|---|
| CPU 亲和性 skill | `subskills/cpu-affinity-optimization/SKILL.md` |
| 网络参数调优 skill | `subskills/network-optimization/SKILL.md` |
| 性能库选型 skill | `subskills/performance-library-selection/SKILL.md` |
| 应用配置参数调优 skill | `subskills/application-config-optimization/SKILL.md` |
| BIOS 优化 skill | `subskills/bios-optimization/SKILL.md` |
| OS 优化 skill | `subskills/os-optimization/SKILL.md` |
| 编译优化 skill | `subskills/compiler-optimization/SKILL.md` |
| GPU/NPU 等计算卡 skill | `subskills/accelerator-optimization/SKILL.md` |
| 更换高规格硬件 | `subskills/hardware-upgrade-analysis/SKILL.md` |
| Other 优化 skill | `subskills/other-optimization/SKILL.md` |
| 非 CPU 瓶颈预筛 | `subskills/io-memory-network-bottleneck-analysis/SKILL.md` |
| 数据库专项 | `subskills/database-workload-analysis/SKILL.md`，由应用配置 skill 按需引用 |

## Framework Ownership Boundary

主框架只维护跨 skill 的公共能力，不承接各专项 skill 的领域细节：

- 主框架负责：用户输入确认、环境诊断备份、基线确认、瓶颈分类、性能信息采集契约、候选优化 skill 列表生成、任务包 schema、候选池合并、执行验证门禁、收益统计、报告、review、环境还原和案例归档。
- 主框架负责：检查每个 subskill 的 `SKILL.md` 是否存在、名称是否与目录一致、是否能按任务包和结果 JSON 契约接入。
- 主框架不负责：重写 CPU 亲和性、网络、编译、BIOS、OS、性能库、GPU/NPU 等专项分析细节；这些内容由对应 subskill 维护者迭代。
- 子 skill 优化建议应作为独立任务下发，不能阻塞主框架发布；只有当子 skill 缺失、命名不一致或输出契约不兼容时，才视为主框架阻塞问题。

## Safety Gates

- 默认从 `analysis_only` 开始；任何真实变更前必须经过 `dry_run` 和用户确认。
- 所有用户确认点都必须是显式确认，不能由“用户要求继续优化”“任务看起来完整”或历史材料自动推断。确认缺失时必须停止在对应门控，输出当前 workflow 阶段、待确认问题和可继续的最小答复。
- 场景与环境摘要确认、BMC/Redfish 采集或跳过确认、环境诊断结果用户确认、服务健康检查、目标实例身份确认和基线确认是候选列表生成前的硬门控；失败或未确认时必须停下并报告，不得使用历史记录绕过。
- 历史日志、历史报告、旧轮次结果只能作为 `historical_records_status=discovered_unconfirmed` 的候选输入；是否用于分析、对比或调优必须由用户显式确认。
- 当前产物优先级高于历史产物：只要本轮服务健康、基线、证据或 run_id 校验失败，必须报告当前异常；禁止用历史成功结果覆盖当前失败状态。
- 所有下游结论必须能追溯到同一个 `current_run_id`。run_id 缺失、不一致或证据时间早于本轮启动时间时，结论状态只能是 `blocked` 或 `degraded`。
- BIOS、OS、网络、防火墙、IRQ、重启、重编译、LD_PRELOAD、远程 SSH、硬件调整建议均属于高风险路径，必须输出风险、验证方法和回退方法。
- 子 skill 在分析阶段不得修改系统；实施只能在主流程的迭代优化阶段串行执行。
- 敏感信息、客户路径、账号、IP 和原始业务数据进入报告前必须脱敏。

## References

### Phase 1 强制读取

- `references/application-agent-architecture.md` — 架构图到 skill 的顶层映射和三阶段重构边界
- `references/input-contract.md` — 用户界面输入、确认项和输出字段
- `references/user-interaction-gates.md` — 启动、BMC/Redfish、环境诊断和基线确认的主动询问机制
- `references/checklist.md` — 自顶向下执行检查清单
- `references/environment-diagnosis.md` — 环境备份后的诊断规则，包括历史 reference 问题集、BIOS 高性能配置、perf/PMU 可用性和内核补丁齐全性

### Phase 2 按需读取

- `references/candidate-skill-list.md` — 根据采集信息生成候选优化 skill 列表的规则
- `references/knowledge-technique-routing.md` — 知识库技术层、案例与当前子 skill 的映射
- `references/prerequisites.md` — 工具依赖、权限和降级策略
- `references/optimization-decision-tree.md` — 优化决策树和变更模式
- `references/candidate-skill-analysis.md` — 候选 skill 分析清单与输出校验
- `references/subagent-orchestration.md` — subagent 任务包、JSON schema 和候选池合并
- `references/platform-tuning-notes.md` — 平台调优笔记
- `references/external-library-replacement-integration.md` — 外部库替换集成
- `references/external-network-io-integration.md` — 外部网络 IO 集成

### Phase 3 按需读取

- `references/iteration-execution.md` — 迭代执行、收益归因、单 skill 停止与全局停止规则
- `references/review-restore-archive.md` — review、环境还原、案例归档
- `references/report-schema.md` — 报告字段契约
- `references/report-generation.md` — 报告生成要求
- `references/examples.md` — 端到端示例

## Compatibility Notes

- 主源目录为 `skills/kpbot-app-tuner/`。
- `.claude/`、`.opencode/`、`.agents/` 中的入口只做发现和跳转；Codex、Cursor 等编程 Agent 可直接加载主源目录，所有行为以本主源为准。
- 仓库内 `ref-skills/` 作为外部能力源，必须通过本 skill 的候选列表和安全门禁接入。
