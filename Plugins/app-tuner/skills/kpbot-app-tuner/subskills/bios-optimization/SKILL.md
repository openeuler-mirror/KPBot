---
name: bios-optimization
description: 分析和调优 BIOS/固件层性能参数，覆盖 Power Profile、SMT、NUMA/Node Interleaving、C-State、Turbo、DDR Speed、Hardware Prefetcher、PCIe ASPM。支持独立运行（自带证据采集）和作为 kpbot-app-tuner 子 skill 两种模式。适用于鲲鹏（916/920/930/950）+ 昆仑 BIOS/AMI Aptio/InsydeH2O 平台。当用户需要检查或优化 BIOS 配置、固件参数、BMC/Redfish 设置时触发。BIOS 变更不直接执行，只生成操作手册；standalone 模式下经用户逐项确认可选执行 Redfish PATCH。
---

# BIOS Optimization

分析和调优 BIOS/固件层参数,适用于鲲鹏 + 昆仑 BIOS/AMI Aptio/InsydeH2O 平台。

## 何时触发

满足任一条件即进入本 skill：

- 用户要求检查 BIOS 配置是否最优（Power Profile/SMT/C-State/DDR Speed 等）
- 用户要求检查 BIOS 是否限制了数据库/应用性能
- 用户要求检查 NUMA/Node Interleaving 设置
- 用户要求检查 BMC/Redfish BIOS 配置
- 用户描述性能问题且证据指向 BIOS 配置（Power Profile=Balanced、Node Interleaving=on、C-State=C6 等）
- 主SKILL 瓶颈分析将 BIOS 列为候选优化方向（coverage skill 始终加入）

## 必读 Reference

按场景加载，避免一次性把所有细节放入上下文：

- 参数推荐值、决策规则、Redfish 属性映射、证据清单、跨 skill 联动、critical 回退：`references/bios-playbook.md`

## Input Modes

本 skill 支持两种入口,后续分析逻辑统一：

### 独立运行（standalone）

- **触发**: 未提供 `evidence_snapshot_dir` 和 `environment_backup_dir`
- **行为**: 调用 `backup_environment.sh` 采集 Redfish + `collect_bios_evidence.sh --supplement` 补采 OS 侧缺口;无 BMC 时调用 `collect_bios_evidence.sh --os-only`
- **必须输入**: 无
- **应用类型**: Agent 从上下文推断（如用户提到数据库/中间件/批处理等）。推断不出时询问用户,用户可选"不清楚",选"不清楚"则按通用推荐值处理
- **压测用例**: 采集配置后、执行变更前询问用户。有压测用例则变更前跑基线、重启后对比收益;无则只验证参数生效
- **执行**: 逐项询问用户执行方式（Redfish PATCH / 手动 / 跳过）

### 主SKLL调用（subagent）

- **触发**: 主SKLL 提供了 `evidence_snapshot_dir` 和/或 `environment_backup_dir`
- **行为**: 直接读取主SKLL 已采集的证据,缺失项调 `collect_bios_evidence.sh --supplement` 补采
- **输入来源**: 主SKLL 任务包
- **执行**: 不执行,输出 candidate_actions 供主SKLL 在 reboot round 处理

> 独立运行时 bios-optimization 完成 Step 0-9 全流程。主SKLL调用时 bios-optimization 只做 Step 0-7（分析+输出 candidate_actions）,Step 8-9 由主SKLL/用户处理。

## Inputs

| 输入 | subagent 模式 | standalone 模式 | 缺失降级 |
|------|-------------|----------------|---------|
| `run_mode` | 主SKLL 显式传入 | 自动推断为 standalone | — |
| `evidence_snapshot_dir` | 主SKLL 提供 | 不需要（自采集） | — |
| `environment_backup_dir` | 主SKLL 提供 | 不需要（自采集） | — |
| `bottleneck_classification` | 主SKLL 提供 | 用户描述（可选） | 无 → 瓶颈归因降级为"不明确" |
| `application_type` | 主SKLL 提供 | Agent 从上下文推断,推断不出用通用推荐值 | 缺失 → 通用推荐值 |
| `REDFISH_BMC_HOST` | 不需要 | 环境变量（可选） | 无 BMC → --os-only,confidence=medium |
| `REDFISH_BMC_USER` | 不需要 | 环境变量（可选） | 同上 |
| `REDFISH_BMC_PASS` | 不需要 | 环境变量（可选） | 同上 |
| `bios_screenshots_dir` | 可选 | 可选 | 无 → 跳过用户手动证据 |
| `system_reboot_allowed` | 主SKLL 提供 | 用户确认 | 无 → 所有动作降级为 analysis_only |
| `agent_action_mode` | 主SKLL 统一授权 | 用户确认 | — |
| `output_dir` | 主SKLL 指定 | 用户指定或默认 `./bios-optimization-output/`（同时作为调优状态持久化目录） | — |

## Evidence Collection

从 `environment_backup_dir` 读取 Redfish JSON 和 OS 侧证据,或通过 `scripts/collect_bios_evidence.sh` 自采集。

**必需证据**（缺失则对应参数跳过或降级）：

- `bios-redfish-bios.json` — Redfish BIOS Attributes（核心,缺失则 Power Profile/Prefetcher 无法获取）
- `bios-info.txt` — BIOS 厂商/版本/服务器型号（dmidecode -t bios/system）
- `hardware-cpu.txt` / `lscpu` — SMT/Turbo 推断
- `hardware-memory.txt` / `dmidecode -t memory` — DDR Speed
- `numa-topology.txt` / `numactl -H` — NUMA/Node Interleaving 推断
- `cstate_info.txt` — C-State（cpupower idle-info,需补采）
- `pcie_aspm_info.txt` — PCIe ASPM（lspci -vvv,需补采）

**无法从 OS 侧获取的证据**（必须有 Redfish 或用户手动提供）：

- Power Profile — 只能从 Redfish 获取
- Hardware Prefetcher — 只能从 Redfish 获取

**证据质量分级**：

| 来源 | 置信度 | 标注 |
|------|--------|------|
| Redfish BIOS Attributes | high | `source=redfish` |
| OS 侧推断 | medium | `source=os_inferred` |
| 用户手动输入 | low | `source=user_manual` |
| 无法获取 | — | `source=unavailable` |

> **证据时效性**: 不检查。BIOS 设置存储在 NVRAM/CMOS,不受运行时进程影响,与 OS SKILL 不同。`current_evidence_status` 默认 `current`。

> **内核版本不敏感**: BIOS 选项由固件决定,与 openEuler 内核版本（OLK-5.10/OLK-6.6）无关,与 OS SKILL 不同。

Redfish 属性名因厂商而异,按 `references/bios-playbook.md` Item 3b 的三级归一化策略匹配（精确 → 关键词模糊 → OS 侧推断降级）。无法匹配时标注 `unable_to_determine`,不得伪造 BIOS 状态。

缺失证据时输出 `required_evidence`,不得直接给出高置信变更建议。

## 强制门控（不可跳过）

> **禁止在完成证据采集前给出任何 BIOS 配置推荐。**
>
> BIOS 参数推荐必须基于明确的当前配置数据,数据来源只允许两种:
> 1. **用户明确提供**: 用户在对话中明确告知当前 BIOS 配置（如"我的 Power Profile 是 Balanced"）
> 2. **Agent 主动采集**: 通过 Redfish 采集或 OS 侧命令（dmidecode/lscpu/numactl 等）获取
>
> playbook 中的经验值是"采集到当前配置后,用来对照判断是否需要变更的目标值",不是"可以直接告诉用户的答案"。
> 例如: 采集到当前 Power Profile=Balanced,再对照 playbook 推荐值 Performance,才能输出"当前 Balanced,推荐改为 Performance"。
> 不允许: 在不知道当前配置的情况下直接推荐"建议设为 Performance"。
> 不允许: 用"可能是""应该是"等模糊措辞替代实际采集结果。
>
> 唯一例外: 用户明确要求"不采集直接推荐"时,可基于 playbook 经验值给出推荐,但必须标注 `source=experience_only, confidence=low`,并提示用户"此推荐未基于实际采集数据,建议采集后重新确认"。

## Workflow

### Step 0: 前置筛查与模式检测

**0a. 模式检测**

- 如果提供了 `evidence_snapshot_dir` 或 `environment_backup_dir` → `run_mode = subagent`
- 否则 → `run_mode = standalone`

Agent 判断完模式后，用 Bash 工具执行以下命令记录日志（不在对话中展示）:

```
echo "[bios-optimization] run_mode=<subagent 或 standalone>" >&2
```

**0b. standalone 模式: 证据采集（必须执行，不可跳过）**

当 `run_mode == standalone` 时，必须先完成证据采集，才能进入后续分析步骤。

**步骤 1: Agent 检查环境变量并拷贝脚本**

Agent 必须用 Bash 工具执行以下命令（拷贝脚本并预置 backup_environment.sh 路径,然后检查环境变量）:

```
cp <skill_dir>/scripts/collect_bmc_credentials.sh /tmp/collect_bmc_credentials.sh && echo "<skill_dir>/../../scripts/backup_environment.sh" > /tmp/.kpbot_backup_script_path
```

```
bash -c 'if [[ -n "${REDFISH_BMC_HOST:-}" && -n "${REDFISH_BMC_USER:-}" && -n "${REDFISH_BMC_PASS:-}" ]]; then echo "SET"; else echo "UNSET"; fi'
```

**步骤 2: 根据检查结果向用户展示**

- 如果第二条命令输出 "SET"，向用户展示:

> 已检测到 BMC 环境变量（REDFISH_BMC_HOST / REDFISH_BMC_USER / REDFISH_BMC_PASS），凭据已就绪。回车继续，或输入 'skip' 跳过。

- 如果第二条命令输出 "UNSET"，向用户展示:

> 未检测到 BMC 环境变量。这些变量需在使用本 SKILL 前设置好（Agent 启动时继承环境，运行中设置的变量无法读取）:
> ```
> export REDFISH_BMC_HOST=<BMC IP>
> export REDFISH_BMC_USER=<用户名>
> export REDFISH_BMC_PASS=<密码>
> ```
> 下次使用前设置即可自动读取。
>
> 本次是否通过脚本交互输入凭据？为保证数据安全，凭据仅在内存中使用，不会写入磁盘或日志。
> 如需使用，请在另一个终端执行:
> ```
> bash /tmp/collect_bmc_credentials.sh --output-dir /tmp/bmc
> ```
> 脚本会依次提示输入 BMC IP、用户名、密码（密码不回显、不落盘），并自动采集 Redfish 数据。完成后回到这里回复 'done'。
>
> 如无 BMC 凭据，输入 'skip' 跳过，将使用 OS 侧推断（部分参数无法获取）。

根据用户回复执行采集:

- 如果环境变量为 SET 且用户回车 → 调用 `backup_environment.sh` 采集 Redfish + `collect_bios_evidence.sh --supplement`
- 如果用户回复 'done' 且 `/tmp/bmc/bios-redfish-bios.json` 存在 → 调用 `collect_bios_evidence.sh --supplement --existing-backup-dir /tmp/bmc`
- 如果用户回复 'skip' 或无 BMC → 调用 `collect_bios_evidence.sh --os-only`

**采集后 BMC 连接失败检查**（如果尝试了 Redfish 采集）:

Agent 读取采集输出目录下的 `.redfish_last_http_code` 文件:

```
cat <backup_dir>/.redfish_last_http_code
```

如果文件存在且内容不为 "200":

- 如果内容为 "000"（BMC 不可达），向用户展示:

> BMC 不可达（网络不通或 BMC 服务未启动），采集 Redfish 数据失败。请检查 BMC IP 是否正确、网络是否连通、BMC 服务是否正常运行。排查后在另一个终端重新执行:
> ```
> bash /tmp/collect_bmc_credentials.sh --output-dir /tmp/bmc
> ```
> 完成后回到这里回复 'done'，或输入 'skip' 使用 OS 侧推断（部分参数无法获取）。

- 如果内容为其他非 200 状态码（如 401/403 等），向用户展示:

> BMC 采集失败（HTTP \<code\>），可能原因：凭据错误、权限不足等。是否重新输入凭据？
> 是 → 请在另一个终端重新执行:
> ```
> bash /tmp/collect_bmc_credentials.sh --output-dir /tmp/bmc
> ```
> 完成后回到这里回复 'done'。
> 否 → 输入 'skip'，将使用 OS 侧推断（部分参数无法获取）。

根据用户回复:
- 如果回复 'done' → 重新调用 `collect_bios_evidence.sh --supplement --existing-backup-dir /tmp/bmc`
- 否则 → 调用 `collect_bios_evidence.sh --os-only`

**0c. 前置筛查（可直接 blocked 退出的场景）**

- 云环境/虚拟化且无 BMC 访问 → `status=blocked`, findings="云环境无法访问 BIOS", `candidate_actions=[]`, 退出
- 非鲲鹏平台 → `status=degraded`, findings="非鲲鹏平台,降级 analysis_only", `candidate_actions=[]`（或通用建议）, 退出
- 其余进入 Step 0d

**0d. 压测用例询问 + 持久化目录选择（仅 standalone 模式，必须在 Step 1 之前执行）**

BIOS 变更需要冷重启，重启后 Agent 进程消失。为支持重启后恢复调优上下文，需要在分析前确认压测用例和持久化目录。

**0d-1: 询问压测用例**

Agent 必须向用户询问:

> 是否有压测用例用于验证 BIOS 优化效果？
> 有 → 请提供压测命令（如 `sysbench --threads=64 oltp_read_only run`）。
>      变更前会先跑一次基线压测并记录结果。重启后执行验证脚本时同时跑压测，自动对比收益。
> 无 → 只验证 BIOS 参数是否生效，不验证性能收益。

根据用户回复:
- 用户提供压测命令 → 记录 `benchmark_cmd`，在 Step 8 执行变更前先跑基线压测
- 用户无压测命令 → 只做参数验证，不包含性能收益对比

**0d-2: 选择持久化目录**

Agent 必须向用户询问:

> BIOS 变更后需要重启，重启后 Agent 会丢失当前对话上下文。
> 请选择一个持久化目录，用于保存调优状态，重启后 Agent 可读取该目录恢复上下文继续调优。
> 默认: `./bios-optimization-output/`
> 可指定其他路径（如 `/tmp/bios-tuning-session/`）:

用户指定目录或回车使用默认目录。该目录用于后续写入 `tuning_session.json`（调优状态文件）。

> 此步骤必须执行，不可跳过。即使用户没有压测用例，也必须确认持久化目录。

### Step 1: 证据获取与补采

```
读取 Redfish JSON 或 OS 侧证据
IF 有缺失项: 调用 collect_bios_evidence.sh --supplement 补采
IF 补采后证据完全缺失:
  → status=blocked, 列出最小补采命令, 退出
IF 部分缺失:
  → status=degraded, 缺失参数标注 unable_to_determine
输出 evidence_status
```

### Step 2: 平台识别 + 固件版本已知限制检查

- **平台**: 读取 `bios-info.txt` / Redfish,确认服务器型号 + BIOS 厂商 + 版本 + 鲲鹏型号（916/920/930/950）。非鲲鹏降级 `analysis_only`。
- **A+K 场景识别（两步判断）**:

  **第 1 步: 判断是否为 AI 训练推理场景**

  Agent 从用户提示词中匹配以下关键词:
  - 框架/工具: PyTorch、torch_npu、MindSpore、MindSpeed、MindSpeed-LLM、MindSpeed-MM、CANN、Transformers、vLLM、DeepSpeed
  - 场景: 模型训练、推理、大模型、大语言模型、fine-tune、预训练、微调、多模态、serving、推理服务
  - 硬件: Atlas、昇腾、Ascend、NPU

  命中任一关键词 → 判定为 AI 训练推理场景，进入第 2 步。
  未命中 → 询问用户应用场景类型（可选"不清楚"），用户回答 AI 训练推理 → 进入第 2 步；否则不检测 A+K，按通用 Decision Matrix 处理。

  **第 2 步: 检测硬件是否为 Ascend NPU + 鲲鹏 CPU**

  Agent 用 Bash 工具执行以下命令检测昇腾 NPU 设备:

  ```
  bash -c 'lspci 2>/dev/null | grep -i "processing accelerators\|d100\|d500\|d801" | head -1'
  ```

  CPU 是否为鲲鹏已在平台识别中确认。

  - NPU 检测到 + CPU 为鲲鹏 → 判定为 A+K 场景，使用 A+K 经验库推荐值（覆盖通用 Compute 类型推荐）
  - NPU 未检测到或 CPU 非鲲鹏 → 不匹配 A+K 场景，按通用 Compute 类型处理

  此检测由 Agent 自动完成，不询问用户。
- **固件版本已知限制**: 对照 `references/bios-playbook.md` Item 18 检查:
  - 930 早期 BIOS C-State 不可配 → C-State `change_mode=analysis_only`
  - 920 某版本不支持 SNC → 不输出 SNC 相关 candidate_action

### Step 3: 当前配置分析 + 整体跳过检查

逐参数读取当前值,对照推荐值（见 `references/bios-playbook.md`）,标记 `already_optimal` / `needs_change` / `unable_to_determine`。

**整体跳过规则**:

```
IF Power Profile == Performance（或 Custom 且各参数已最优）
   AND SMT 设置正确（920 无 SMT 跳过,930/950 按推荐值）
   AND Node Interleaving == off
   AND C-State 限制为 C0 或 C1（不允许 C6 及更深;OLTP）或 OS controlled（Batch）
   AND DDR Speed == 标称最高
   AND PCIe ASPM == off（数据库场景）
THEN
   status=ok, confidence=high, findings="BIOS 参数已最优"
   candidate_actions=[], 跳过 Step 4-7
```

### Step 4: 瓶颈归因

BIOS 当前配置是否能解释瓶颈?

| 瓶颈类型 | BIOS 归因 | 建议 |
|---------|---------|------|
| CPU 瓶颈 + Power Profile=Balanced | ✅ | 改 Performance |
| 内存瓶颈 + DDR 降频 | ✅ | 恢复标称最高 |
| NUMA 瓶颈 + Node Interleaving=on | ✅ | 关闭 Interleaving |
| 延迟瓶颈 + C-State=C6 | ✅ | 改 C1 |
| 无关瓶颈 | ❌ | BIOS 非主因,降级为通用检查 |

### Step 5: 依赖检查

参数间联动（见 `references/bios-playbook.md` Item 17）:

- Power Profile=Performance → 自动限制 C-State + Turbo on
- SMT off → 逻辑核数减半 → NUMA 拓扑变化
- Custom Power Profile → 各参数独立判断

### Step 6: 动作分类 + 风险分级 + 密码/锁定标注

- **change_mode**: `system_reboot`（多数）/ `online`（极少数 Power Profile 在线切换）/ `analysis_only`
- **risk**: `low` / `medium` / `high` / `critical`
- **critical** 操作: `change_mode=analysis_only`,不生成 Redfish PATCH
- **BIOS 密码/厂商锁定**:
  - `bios_password_required=true` → 不生成 PATCH,手册标注"需 BIOS 密码"
  - `locked_by_vendor=true` → 降级 `analysis_only`,不进 candidate_actions

### Step 7: 输出候选动作 + 依赖联动标注

输出 `candidate_actions` + `bios_findings` + `required_evidence` + `risk_notes` + 回退计划 + 重启后验证清单。

**依赖联动标注**（在 `implementation_plan` 中附加）:
- `dependent_action_ids`: 本 action 生效后联动的 action_id 列表
- `triggered_by`: 本 action 被哪些 action 联动触发

> Step 7 是 subagent 模式分析阶段的最后一步。Step 8-9 按模式分叉。

### Step 7.5: 写入调优状态（仅 standalone 模式，在 Step 7 输出候选动作后执行）

Agent 将以下信息写入 Step 0d 确认的持久化目录下的 `tuning_session.json`:

```json
{
  "resume_prompt": "继续 BIOS 调优,状态目录: <持久化目录>",
  "session_id": "<唯一ID>",
  "skill": "bios-optimization",
  "run_mode": "standalone",
  "status": "pre_reboot",
  "created_at": "<ISO 8601>",
  "user_prompt": "<用户最初的提示词>",
  "application_type": "<应用类型>",
  "platform": {
    "cpu_model": "<鲲鹏型号>",
    "bios_vendor": "<BIOS厂商>",
    "bios_version": "<BIOS版本>"
  },
  "evidence": {
    "evidence_dir": "<证据目录>",
    "redfish_available": true/false
  },
  "candidate_actions": [/* Step 7 输出的候选动作 */],
  "executed_actions": [
    {
      "action_id": "<id>",
      "title": "<标题>",
      "category": "<类别>",
      "method": "redfish_patch|manual|skipped",
      "pre_change_value": "<变更前值>",
      "target_value": "<目标值>",
      "result": "success|failed|skipped",
      "http_status": 200
    }
  ],
  "benchmark": {
    "provided": true/false,
    "cmd": "<压测命令>",
    "baseline_result": {/* 基线压测结果 */},
    "threshold_pct": 3,
    "decision_rule": "gain>=threshold → 保留; 0<gain<threshold → 询问; gain<0 → 回退"
  },
  "rollback_plan": [/* 每个已执行动作的回退步骤 */],
  "next_step": "reboot_then_verify",
  "next_step_instruction": "重启服务器后,开新会话,发送 resume_prompt 字段的内容即可恢复调优"
}
```

> 同时将 `post_reboot_verify.sh` 和 `pre_change_snapshot.json` 拷贝到该目录。

### Step 8: 执行决策（按模式分叉）

```
subagent 模式:
  → 不执行,输出 candidate_actions 供主SKLL 在 reboot round 处理

standalone 模式:
  0. 如果 Step 7.5 提供了压测用例:
     → 执行基线压测,将结果写入 tuning_session.json 的 benchmark.baseline_result

  1. 调用 apply_bios_change.sh --generate 生成所有文件
  2. 逐项询问用户: [R] Redfish PATCH / [M] 手动 / [S] 跳过
     - critical 项: 只提供 [M] / [S],不提供 [R]
     - locked_by_vendor 项: 不进入询问
  3. R → 展示 PATCH body → 用户确认:
     a. 先调用 apply_bios_change.sh --execute-patch --action-id <id> --dry-run
        输出将执行的 PATCH body 和目标 URL,不实际执行
     b. 用户确认 Y/N
     c. Y → 调用 apply_bios_change.sh --execute-patch --action-id <id> [--force]
        - risk=high 时必须加 --force 才会执行
        - risk=critical 拒绝执行（即使用户要求）
     d. 记录执行结果（HTTP 状态码）
  4. M → 记录到 bios_change_plan.md
  5. S → 标注 skipped
  6. 根据 dependent_action_ids 处理联动
  7. 更新 tuning_session.json: 将执行结果写入 executed_actions, status 改为 "pre_reboot"

  重启后指引（Step 8 末尾向用户展示）:
    "BIOS 变更已完成。调优状态已保存到: <持久化目录>

     请执行以下步骤:
     1. 重启服务器
     2. 重启后开新会话,发送以下提示词恢复调优（已保存在 tuning_session.json 的 resume_prompt 字段）:

        ┌─────────────────────────────────────────────────────────────┐
        │ 继续 BIOS 调优,状态目录: <持久化目录>                        │
        └─────────────────────────────────────────────────────────────┘

     3. Agent 会读取状态目录恢复上下文,执行验证脚本（含参数比对和压测收益对比）,
        根据收益决定保留或回退,无需你重新解释当前情况"
```

### Step 9: 输出（按模式分叉）

```
subagent 模式:
  → 输出 candidate_actions JSON（符合 subagent-orchestration.md 契约）

standalone 模式:
  → 输出 tuning_report.json + 终端总结
  → 提示用户重启后开新会话恢复调优（给出状态目录路径和恢复指令）
```

### Step 10: 重启后恢复（仅 standalone 模式，新会话触发）

当用户在新会话中说"继续 BIOS 调优"或类似表述时，Agent 执行以下恢复流程:

**10a: 读取调优状态**

Agent 读取用户指定的状态目录下的 `tuning_session.json`:

```
cat <状态目录>/tuning_session.json
```

**10b: 恢复上下文**

Agent 从 `tuning_session.json` 中恢复:
- 之前采集的证据、平台信息、应用类型
- 已执行的变更（哪些参数改了、从什么值改成什么值）
- 基线压测结果（如果有）
- 待执行的下一步（`next_step`）
- 回退方案

**10c: 执行验证**

Agent 执行状态目录下的验证脚本:

```
cd <状态目录> && bash ./post_reboot_verify.sh pre_change_snapshot.json
```

验证脚本完成:
- 参数比对: 采集当前 BIOS 配置与变更前快照对比
- 压测对比（如有基线）: 执行压测命令,与基线对比计算收益百分比

**10d: 收益判定与决策**

Agent 根据验证结果决策:
- 所有参数已生效 AND 收益达标（gain >= threshold）→ 确认保留,更新 `tuning_session.json` status 为 `completed`
- 参数已生效但收益不达标（0 < gain < threshold）→ 向用户展示结果,询问是否保留
- 收益为负（gain < 0）→ 建议回退,展示回退方案（从 `rollback_plan` 读取）
- 部分参数未生效 → 标注未生效项,建议手动检查 BIOS Setup

**10e: 更新状态**

Agent 更新 `tuning_session.json`:
- `status` 改为 `completed` / `rolled_back` / `partial`
- 写入验证结果和最终决策
- 如需多次重启（例如回退后重新调整），`status` 改为 `pre_reboot`,`next_step` 改为对应步骤

## Decision Matrix

> 此矩阵为参考基线,实际推荐值由平台分支和 Power Profile 模式决定。详细决策规则见 `references/bios-playbook.md`。

| BIOS Setting | Database OLTP | Compute | RPC/Latency | Batch |
|---|---|---|---|---|
| Power Profile | Performance | Performance | Performance | Performance / Custom |
| SMT / Hyper-Threading | Off 或按实测 | On | Off 或按实测 | On |
| NUMA / Node Interleaving | Off | Off | Off | Off |
| C-State Limit | C0 或 C1 | C6 可接受 | C0 或 C1 | OS controlled |
| Hardware Prefetcher | On | On | On | On |
| Turbo Boost | On（930/950） | On | On | On |
| DDR Speed | Max supported | Max supported | Max supported | Max supported |
| PCIe ASPM | Off | Off 或按实测 | Off | Off 或 On |

**平台分支**:
- 鲲鹏 920 无 SMT → 跳过 SMT 参数
- 鲲鹏 916/920 无 Turbo → 跳过 Turbo 参数
- ARM 无 Energy Performance Bias → 跳过,标注"不适用"

**Power Profile 模式分支**:
- Performance → 其他参数从 profile 默认值派生
- Custom → 各参数独立判断
- Balanced/PowerSave → 推荐改为 Performance 或 Custom

## Dependencies

| 工具 | 用途 | 必需性 | 缺失影响 |
|------|------|--------|---------|
| `dmidecode` | BIOS 厂商/版本/型号/DDR Speed | 必需（root） | 全量降级 |
| `lscpu` | SMT/Turbo 推断 | 必需 | SMT/Turbo 参数跳过 |
| `numactl` | NUMA/Node Interleaving 推断 | 必需 | NUMA 参数跳过 |
| `cpupower` | C-State 信息 | 可选（root） | C-State 降级为 sysfs |
| `lspci` | PCIe ASPM | 可选 | PCIe ASPM 参数跳过 |
| `curl` | Redfish 采集（backup_environment.sh） | 可选 | 无 Redfish,降级 OS 侧推断 |
| `python3` | JSON 解析 + manifest 生成 | 必需 | 全量降级 |

## Platform Notes

BIOS SKILL 对 openEuler 内核版本（OLK-5.10/OLK-6.6）不敏感——BIOS 选项由固件决定,与内核无关。只需区分鲲鹏型号和 BIOS 厂商。

| 型号 | SMT | Turbo | Power Profile 影响 | DDR | 特殊注意 |
|------|-----|-------|-------------------|-----|---------|
| 916 (0xd01) | ❌ 无 | ❌ 无 | 主要影响 C-State | DDR4 2400 固定 | — |
| 920 (0xd01) | ❌ 无 | ❌ 无 | 主要影响 C-State | DDR4 2666/2933/3200 | 某版本不支持 SNC |
| 930 (0xd03) | ✅ SMT2 | 受限 | 影响 C-State/Turbo | DDR4 2933/3200 | 早期 BIOS C-State 不可配 |
| 950 (0xd06) | ✅ SMT2 | ✅ 动态 | **影响最大**（1.2-2.3GHz） | DDR5 4800/5600 | Power Profile 优先级最高 |

| BIOS 厂商 | 识别方式 | Redfish 属性名风格 | 在线切换 Power Profile |
|---------|---------|-------------------|----------------------|
| 昆仑 BIOS | dmidecode → "Kunpeng"/"Huawei" | `WorkloadProfile`/`ProcessorCstate` | 部分版本支持 |
| AMI Aptio | dmidecode → "American Megatrends" | `SystemProfile`/`CStateCtl` | ❌ 需冷重启 |
| InsydeH2O | dmidecode → "Insyde" | `PowerProfile`/`CstateEnable` | ❌ 需冷重启 |

## Risk Assessment

| 风险等级 | 操作类型 | 示例 | 回退方式 |
|---------|---------|------|---------|
| low | 可在线生效 | 部分服务器 Power Profile | 恢复原值 |
| medium | 冷重启可恢复 | SMT/C-State 限制 | 再次重启恢复 |
| high | 冷重启恢复需谨慎 | DDR Speed 降频恢复/NUMA 拓扑 | 记录原值,验证拓扑 |
| **critical** | **可能无法启动** | DDR 超标称 overclock/关关键内存通道 | **硬件级恢复** |

critical 操作的硬件级回退:
1. CMOS 清除（物理跳线）→ 恢复出厂默认
2. iBMC/IPMI 远程恢复 → BMC Web 恢复 BIOS
3. 备用 BIOS 芯片切换 → 切换到 Backup BIOS

**约束**: critical 操作默认 `change_mode=analysis_only`;standalone 模式不提供 Redfish PATCH 自动执行;`rollback` 含 `boot_failure_recovery` 字段。

## Post-Reboot Validation

BIOS 变更需重启后验证。重启后 SKILL 进程已消失,验证脚本已由 `apply_bios_change.sh --generate` 拷贝到 `output_dir`,用户在 `output_dir` 内手动执行即可:

```bash
# 在 output_dir 内执行,自动采集当前配置并与变更前快照比对
cd <output_dir> && bash ./post_reboot_verify.sh pre_change_snapshot.json
```

验证脚本自动比对以下参数（变更前 vs 变更后）:

| 参数 | OS 侧验证方式 | Redfish 依赖 |
|------|-------------|-------------|
| SMT | `cat /sys/devices/system/cpu/smt/active` | ❌ |
| NUMA/Interleaving | `numactl -H` 节点数 | ❌ |
| C-State | cpuidle sysfs 最深状态 | ❌ |
| Turbo | cpufreq max/min | ❌ |
| DDR Speed | `dmidecode -t memory` | ❌ |
| PCIe ASPM | `lspci -vvv` LnkCtl | ❌ |
| Power Profile | 无法 OS 侧获取 | ✅ 需 Redfish |
| Hardware Prefetcher | 无法 OS 侧获取 | ✅ 需 Redfish |

如需回退,使用 `apply_bios_change.sh --rollback` 从 `pre_change_snapshot.json` 生成回退 PATCH body（Agent 需先将脚本拷贝到 `output_dir` 或告知用户脚本所在路径）:

```bash
bash ./apply_bios_change.sh --rollback --action-id <id> \
  --candidate-actions <file> \
  --existing-backup-dir .
```

将验证结果回传给主SKLL（subagent 模式）或在对话中报告（standalone 模式）。

## Reject Conditions

候选出现以下任一情况应跳过或降级:

- 参数已为推荐值（跳过）
- 平台不支持该参数（920 无 SMT/Turbo/EPB → 跳过）
- 云环境/虚拟化无 BMC → `status=blocked`
- 容器环境无 BIOS 访问 → `status=blocked`,建议通过宿主机 BIOS 操作
- 非鲲鹏平台 → `status=degraded`,`analysis_only`
- 证据完全缺失 → `status=blocked`
- 厂商锁定（Redfish `ReadOnly=true`）→ 降级 `analysis_only`
- critical 风险操作 → 默认 `analysis_only`
- 生产环境不允许重启 → 仅输出建议
- 整体已最优（见 Step 3 整体跳过规则）→ `status=ok`
- 固件版本已知限制（见 Step 2）→ `change_mode=analysis_only` 或跳过

## Interaction Notes

BIOS 变更会改变其他 subskill 的输入前提,需在 findings 中标注联动影响:

| BIOS 变更 | 影响 | findings 标注 |
|---------|------|--------------|
| SMT on↔off | `cpu-affinity-optimization` | `requires_cpu_affinity_rerun=true`（逻辑核数变化） |
| NUMA/Interleaving 变更 | `cpu-affinity-optimization` + `os-optimization` | `requires_cpu_affinity_rerun=true`, `requires_os_optimization_rerun=true` |
| C-State 限制变更 | `os-optimization` | governor 效果与 C-State 相关 |
| DDR Speed 变更 | `os-optimization` | `requires_os_optimization_rerun=true`（带宽变化影响 HugePages/THP） |
| Power Profile 变更 | `os-optimization` | Performance 可能自动设 governor,避免重复调优 |

subagent 模式: 主SKLL 在 reboot round 完成后应提示重跑 `cpu-affinity-optimization` 和 `os-optimization`。
standalone 模式: 终端总结中提示"BIOS 变更后,建议重新检查 OS 参数和绑核方案"。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含以下字段（符合 `subagent-orchestration.md` 契约）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | string | 唯一标识 |
| `title` | string | 简短标题 |
| `category` | string | `power_profile`/`smt`/`numa`/`cstate`/`turbo`/`ddr_speed`/`prefetcher`/`pcie_aspm` |
| `priority` | string | `high` / `medium` / `low` |
| `change_mode` | string | `analysis_only` / `system_reboot` / `online` |
| `requires_root` | boolean | Redfish PATCH 执行 `requires_root=false`;采集需 root 但不影响此字段 |
| `risk` | string | `low` / `medium` / `high` / `critical` |
| `implementation_plan` | string | BIOS Setup 路径 或 Redfish PATCH body |
| `validation_plan` | string | 重启后验证方法 |
| `rollback` | string | 回退步骤 + critical 场景硬件恢复方案 |
| `expected_effect` | string | 预期效果描述 |
| `expected_gain_metric` | object | `{"metric": "tps", "expected_gain_pct": "3-8%"}` |
| `rejection_criteria` | string[] | `["boot_failure", "post_reboot_validation_failed"]` |
| `evidence_refs` | string[] | 证据路径 |

BIOS 特有约束:
- `risk=critical` → `change_mode=analysis_only`,不生成 Redfish PATCH
- `locked_by_vendor=true` 或 `bios_password_required=true` → 不进入 `candidate_actions`
- `rollback` 对 critical 操作含 `boot_failure_recovery`（`cmos_clear`/`ibmc_remote`/`backup_chip`）

## Outputs

**subagent 模式** — 必须输出以下顶层字段（符合 subagent-orchestration.md 契约）：

- `subskill_name` — 必须为 `bios-optimization`
- `current_run_id` — 必须与任务包一致
- `current_evidence_status` — 默认 `current`（BIOS 不做时效性检查）
- `status` — `ok` / `degraded` / `blocked` / `failed`
- `confidence` — `high`（Redfish）/ `medium`（OS 侧推断）/ `low`（用户手动）
- `analysis_timestamp` — ISO 8601
- `evidence_sources` — 引用的证据路径数组
- `findings` — 结构化发现:
  - `bios_findings` — BIOS 配置分析结果
  - `system_reboot_actions` — 需系统重启的动作清单
  - `analysis_only_actions` — 仅建议不可执行的动作清单（locked/critical/云环境）
  - `cross_skill_impact` — 跨 skill 联动影响标注
- `candidate_actions` — 候选动作数组（结构见 Candidate Action Contract）
- `required_evidence` — 缺失证据（无缺失时为空数组）
- `fallback_notes` — 降级说明（无降级时为空数组）
- `timing` — 耗时统计,至少包含 `analysis_seconds`

若证据不足,输出 `status=degraded|blocked`,并列出最小补采命令;不要把缺失证据解释成"BIOS 无瓶颈"。

**standalone 模式** — 输出 tuning_report.json:

```json
{
  "run_mode": "standalone",
  "executed_actions": [{"action_id": "...", "method": "redfish_patch", "result": "success", "http_status": 200}],
  "manual_actions": [{"action_id": "...", "reason": "critical_risk"}],
  "skipped_actions": [{"action_id": "...", "reason": "user_skipped"}],
  "cross_skill_impact": {"requires_cpu_affinity_rerun": false, "requires_os_optimization_rerun": true},
  "output_files": ["bios_change_plan.md", "bios_redfish_patch.json", "post_reboot_verify.sh", "pre_change_snapshot.json"],
  "summary": "已执行 1 项,已生成手册 1 项,已跳过 1 项",
  "next_steps": "1.完成手动项 2.重启 3.在输出目录执行 bash ./post_reboot_verify.sh pre_change_snapshot.json"
}
```

同时在对话中给出终端总结。

## Boundary

- OS governor、THP、HugePages、sysctl、irqbalance、ulimit、I/O scheduler 由 `os-optimization` 负责
- 线程绑核/NUMA 绑定/cpuset 由 `cpu-affinity-optimization` 负责
- 网卡队列/RSS/RPS/TCP 协议栈由 `network-optimization` 负责
- 应用参数（线程数/连接池/buffer pool）由 `application-config-optimization` 负责
- 编译选项/LTO/PGO 由 `compiler-optimization` 负责
- 本 skill 只输出 BIOS/BMC/固件层候选动作
