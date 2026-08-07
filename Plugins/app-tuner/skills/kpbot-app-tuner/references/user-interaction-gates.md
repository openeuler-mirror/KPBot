# 用户交互门控 / User Interaction Gates

本文定义主 Agent 必须主动询问用户的关键节点。所有询问结果都必须写入本轮 manifest、报告输入或 checkpoint；不能只保存在对话上下文中。

---

## Rule: 关键节点必须向用户确认

当流程到达以下门控时，必须暂停并向用户确认。

### 平台工具强制要求

**Claude Code 平台必须使用 `AskUserQuestion` 工具**实现所有用户确认门控。禁止用纯文本输出代替交互式确认框。

AskUserQuestion 调用规范：
- 每个确认问题设置 `question`（完整问题句）、`header`（≤12 字符的标签）、`options`（2-4 个选项，每项含 `label` 和 `description`）
- `multiSelect: false`（单一选择）
- 选项 label 中标注推荐项，如 `"baseline_first（推荐）"`
- 用户选择后，将结果写入 `gate_confirmation_log`，然后继续流程

非 Claude Code 平台（Codex、Cursor、OpenCode 等）若不支持 `AskUserQuestion`，必须在门控处输出：
- 当前 workflow 阶段和门控名称
- 待确认的完整上下文（摘要、选项说明、风险）
- 明确的选项列表（带编号）
- 提示"请回复选项编号或关键词以继续"
- 在获得显式文本确认前不得进入下一步

- 场景与环境摘要
- 调优入口模式选择
- Agent 操作边界与授权范围
- 多节点清单
- BMC/Redfish BIOS 配置采集（每物理节点）
- 容器目标与进入方式
- 环境诊断结果
- 基线数据与用例

## Required workflow

1. **展示信息**：向用户呈现当前门控所需的完整上下文（摘要、选项说明、风险、后续影响）。
2. **提问确认**：使用统一的选项格式提问；涉及模式选择时，必须逐项说明每种模式的含义、适用场景和后续门控。
3. **等待显式答复**：用户未回复、回复含糊、只给测试报告、只要求"继续优化"或只授权远程执行，都不能等同于确认。必须停在当前门控并展示可接受的最小答复。

## 显式确认硬规则

以下状态不能由 Agent 自行推断，必须得到用户明确答复后才能进入下一步：

- `scenario_confirmation_status = confirmed`
- `optimization_entry_mode_confirmation_status = confirmed`
- `agent_action_confirmation_status = confirmed`
- 每个物理节点的 `bmc_redfish_confirmation_status = confirmed`
- `environment_diagnosis_confirmation_status = confirmed`
- `baseline_confirmation_status = confirmed`

---

## 1. 场景与环境摘要确认

### 确认前必须展示的信息

Agent 从用户提供的测试报告、部署说明或历史结果中抽取出 `scenario_environment_summary`，至少包含：

- 应用名、版本、workload、目标指标和目标规格。
- 部署组网、涉及节点、节点角色、登录方式和压测入口。
- 每个节点的 OS/Kernel、CPU/NUMA、内存、磁盘、网卡、容器/虚拟化、关键应用路径。
- 目标实例身份候选：端口、PID、容器名、配置路径、数据目录、运行用户。
- 压测命令、并发、预热、正式时长、错误率口径。
- 已发现的历史结果或历史配置，标记为 `historical_records_status=discovered_unconfirmed`。
- 缺失项：BMC/Redfish、权限、账号、脚本路径、目标规格约束等。

同时记录 `scenario_input_state`、`environment_info_input_state` 和 `missing_input_items`。

### 确认问题

> 上述场景和环境摘要是否可作为本轮输入？

选项：
- **继续执行** → 写入 `scenario_confirmation_status=confirmed`，进入下一步
- **指出错误或补充** → 更新摘要后重新确认，写入 `scenario_confirmation_status=rebuild_required`
- **取消** → 停止流程，写入 `scenario_confirmation_status=rejected`

只有用户明确选择"继续执行"后，才能进入环境备份或压测。

---

## 2. 调优入口模式选择

### 确认前必须展示的信息

Agent 必须解释三种入口模式：

| 模式 | 适用场景 | 后续门控 | 不会自动执行的动作 |
|------|----------|----------|-------------------|
| `baseline_first` | 还没有可信本轮基线，需先统一资源规格、压测命令和目标实例身份 | 服务健康检查 → 正式基线 → 瓶颈识别 → 调优 | 不会跳过基线；不会用历史数据替代 |
| `running_app` | 服务已在线，先做环境备份和健康检查，再决定基线 | 环境备份 → 健康检查 → 目标实例身份确认 → 用户确认基线策略 | 不会把运行中观测数据直接当作正式基线 |
| `historical_baseline_review` | 已有历史基线或报告，先确认材料是否适用于当前场景 | 历史记录确认门控 → 用户决定是否复用或重建基线 | 历史记录确认前只能做背景参考 |

默认推荐 `baseline_first`（从可信基线开始，路径最清晰）。

### 确认问题

> 请选择调优入口模式：

选项：
- **baseline_first**（推荐）→ 先建立本轮基线，再瓶颈识别和调优
- **running_app** → 先做服务健康检查和目标实例身份确认
- **historical_baseline_review** → 先确认历史材料是否适用
- **取消** → 停止流程

选择后写入 `optimization_entry_mode_confirmation_status=confirmed`。

路由规则：

- `baseline_first`：环境诊断确认后 → 服务健康检查 → 正式基线 → 等待用户确认基线。
- `running_app`：环境诊断确认后 → 确认运行中实例身份和健康状态 → 用户决定基线策略。
- `historical_baseline_review`：历史记录只能做候选输入；用户确认前不得替代现场基线、瓶颈识别或收益统计。

---

## 3. Agent 操作边界确认

### 确认前必须展示的信息

Agent 必须解释三种操作模式：

| 模式 | 允许 | 禁止 |
|------|------|------|
| `analysis_only` | 只读采集、分析、生成建议 | 不执行配置变更、不重启、不重编译 |
| `dry_run` | 生成命令、实施计划、验证计划、回退计划 | 不真实改动环境 |
| `approved_execute` | 在已确认的 change_scope、权限、验证窗口和回退方案内执行真实变更 | 超出边界的新动作必须回到用户确认门控 |

还需确认：允许变更范围（`app_config` / `network` / `os` / `bios` / `compiler` / `library` / `hardware_advice`）、是否允许重启服务、系统重启、重编译、远程 SSH 执行、硬件调整建议。

### 确认问题

> 请选择 Agent 操作模式，并确认允许的变更范围和权限：

选项：
- **继续执行（当前设置）** → 写入 `agent_action_confirmation_status=confirmed`，按已确认边界执行
- **调整边界** → 修改 change_scope 或权限项后重新确认
- **取消** → 停止流程

基线确认后，若候选动作均落在已确认边界内，后续不得再按每个 skill 或每轮动作重复询问用户是否批准。需要新增权限、风险升级、变更范围扩大或用户明确要求人工审批时，才再次询问。

---

## 4. 多节点清单确认

### 确认前必须展示的信息

涉及多台机器时，Agent 必须展示 `node_inventory`：

- 每台机器的 `node_id`、角色、地址、登录用户、是否物理机、是否容器宿主机。
- 哪些节点需要运行统一环境备份和诊断。
- 哪些节点需要 BMC/Redfish 只读 BIOS 采集。
- 哪些节点只做压测客户端健康检查，哪些节点承载目标服务。

### 确认问题

> 上述节点清单和角色分配是否准确？

选项：
- **继续执行** → 写入 `node_inventory_confirmation_status=confirmed`
- **修正** → 更新清单后重新确认，写入 `rebuild_required`
- **取消** → 停止流程

未确认前不得只挑服务端采集，也不得跳过客户端诊断。

---

## 5. BMC/Redfish BIOS 配置采集（每物理节点）

### 确认前必须展示的信息

Agent 必须按物理节点逐台询问。展示该节点的 ID、角色、地址。

若用户选择采集，继续询问：

- BMC 地址或 IP。
- BMC 账号或用户名。
- BMC 密码、token 或由用户自行设置的环境变量/密钥方式。
- 是否允许通过 Redfish 读取 BIOS 属性。

### 确认问题（每节点）

> 节点 `<node_id>`（`<role>`）是否需要采集 BIOS 配置？

选项：
- **采集** → 请提供 BMC 地址、账号、密码/token 传递方式
- **跳过** → 写入 `bmc_redfish_collection_status=skipped_by_user`，BIOS 诊断将降级
- **提供更多信息** → 补充环境变量或凭据传递方式后采集
- **取消** → 停止流程

### 安全规则

- Agent 不得在报告、manifest、日志摘要或案例归档中输出明文密码/token。
- 若凭据通过对话提供，只能用于本次命令调用；推荐通过 `BMC_HOST`、`BMC_USER`、`BMC_PASS` 或脚本参数传入。
- 缺少 BMC/Redfish 证据时，BIOS 高性能配置诊断标记为 `degraded`。
- 多节点场景必须写入 `per_node_bmc_redfish_status[]`。未询问某节点时该节点状态为 `not_asked`，流程必须停止在 `bmc_redfish_confirmation`。

---

## 6. 容器目标确认

### 确认前必须展示的信息

当目标进程运行在容器、Pod、chroot 或进程命名空间中时，Agent 必须展示：

- 容器运行时和进入方式：`docker exec`、`podman exec`、`kubectl exec`、`nsenter` 或不可进入。
- 宿主机采集范围：硬件、内核、PMU、网卡、IRQ、cgroup。
- 容器内采集范围：应用配置、进程视角、用户态依赖、数据库状态、配置文件和数据目录。

### 确认问题

> 容器 `<container_name>` 的操作方式和采集范围是否正确？

选项：
- **继续执行** → 按确认的容器进入方式执行，优先容器内采集
- **提供修复方案** → 如果是容器不可进入，提供替代进入方式
- **降级继续** → 无法进入容器时记录 `container_access_status=blocked|degraded`，仅做宿主机采集
- **取消** → 停止流程

---

## 7. 环境诊断结果确认

### 确认前必须展示的信息

环境诊断完成后，Agent 必须展示：

- 诊断状态、阻塞项、降级项。
- BIOS、perf/PMU、内核补丁、历史问题集检查状态（每项带 `passed` / `failed` / `degraded` / `skipped` 标记）。
- 受影响的后续采集能力和证据路径。
- 最小修复或补采建议。
- 多节点场景：每台机器诊断摘要 + 跨节点风险汇总。

### 确认问题

> 环境诊断结果如上。是否继续后续流程？

选项：
- **继续执行** → 写入 `environment_diagnosis_confirmation_status=confirmed`，进入服务健康检查
- **修复后重诊** → 写入 `rebuild_required`，停在 `environment_diagnosis_confirmation`，等待用户修复后重新诊断
- **取消** → 写入 `rejected`，不得进入服务健康检查或基线

用户确认前，不得进入服务健康检查、目标实例身份确认、正式基线和基线性能确认。

---

## 8. 基线数据确认

### 确认前必须展示的信息

正式基线测试完成后，Agent 必须展示：

- 基线指标（TPS/QPS/P95/吞吐/错误率）、原始日志路径和资源利用率。
- 压测命令、运行时长、并发/线程、预热策略、错误率。
- 测试组网、目标规格约束和目标实例身份（端口、PID、二进制、配置、容器）。
- 测试用例信心与是否符合用户期望场景。

### 确认问题

> 上述基线数据是否可作为本轮调优依据？

选项：
- **继续执行** → 写入 `baseline_confirmation_status=confirmed`，进入瓶颈识别和深度证据采集
- **重跑基线** → 写入 `rebuild_required`，回到基线测试（可调整参数）
- **调整规格** → 写入 `rebuild_required`，修改目标规格或用例后重新建立基线
- **取消** → 停止流程，不得进入瓶颈识别或任何优化动作

### 历史记录处理

发现历史日志、历史报告或旧轮次结果时，标记为 `historical_records_status=discovered_unconfirmed`，并在基线展示时列出。使用前必须确认：

> 发现以下历史记录，是否可用于本轮分析？

选项：
- **确认可用** → 写入 `historical_records_user_confirmation=confirmed`，限定使用范围
- **仅做背景参考** → 不得替代服务健康检查、现场基线、瓶颈识别或收益统计
- **忽略** → 写入 `user_rejected`，本轮不使用

---

## 硬规则补充

- 用户没有回复、回复含糊、只给出测试报告、只要求"继续优化"或只授权远程执行，都不能等同于确认。此时必须展示当前 workflow 阶段、阻塞门控、待确认项和下一步可接受的最小答复。
- 所有确认选择必须写入 `gate_confirmation_log`（提问、答复摘要、状态和写入时间）。
- 当前产物优先级高于历史产物：只要本轮服务健康、基线、证据或 run_id 校验失败，必须报告当前异常；禁止用历史成功结果覆盖当前失败状态。
- 真实危险动作仍必须处于 `approved_execute` 且有回退计划。
