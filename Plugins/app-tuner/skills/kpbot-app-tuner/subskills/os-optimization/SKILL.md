---
name: os-optimization
description: 分析和调优操作系统层性能参数，覆盖 governor、THP、HugePages、numa_balancing、irqbalance、sysctl、ulimit、I/O scheduler、tuned/A-Tune、GRUB 调度参数和容器边界。支持独立运行（自带证据采集）和作为 kpbot-app-tuner 子 skill 两种模式。适用于鲲鹏（916/920/930/950）+ openEuler（22.03/24.03）平台。当用户需要检查或优化 OS 内核参数、系统配置、大页、中断、调度器等 OS 层性能时触发。也覆盖大页内存优化（malloc 使用大页、GLIBC_TUNABLES、glibc 动态库大页、tmpfs 大页）。
---

# OS Optimization

分析和调优 Linux 内核态与 OS 服务层参数,适用于鲲鹏 + openEuler 平台。

## 何时触发

满足任一条件即进入本 skill：

- 用户要求检查或优化 OS 内核参数（governor/THP/HugePages/swappiness/dirty_ratio 等）
- 用户询问大页内存优化（malloc 使用大页、GLIBC_TUNABLES、glibc 动态库大页、tmpfs 大页等）
- 用户要求检查 sysctl 配置是否适合数据库/应用场景
- 用户要求检查 numa_balancing / irqbalance / I/O scheduler 配置
- 用户要求检查 ulimit（nofile/nproc）是否满足应用需求
- 用户要求检查 tuned / A-Tune profile 是否合适
- 用户要求检查 GRUB 启动参数（kpti/mitigation/sched_steal）
- 用户描述性能问题且证据指向 OS 配置（governor 非 performance、THP=always、swappiness 过高等）
- 主SKILL 瓶颈分析将 OS 列为候选优化方向（coverage skill 始终加入）

## 必读 Reference

按场景加载，避免一次性把所有细节放入上下文：

- 参数推荐值、决策树、HugePages 计算、容器边界、协同时序、证据清单：`references/os-playbook.md`

> **⚠️ 涉及大页内存优化（THP/HugePages/malloc 大页/glibc 动态库大页）时，必须加载 `references/os-playbook.md` 的 A+K 场景大页内存相关优化章节。**
> 大页相关参数的版本要求、配置命令、兼容性均以经验库为准，不得引用互联网上的 glibc 社区版本信息。
> 特别是 glibc >= 2.34 即支持 GLIBC_TUNABLES 大页特性（昇腾官方文档确认），不得将版本门槛提高到 2.35。

## Input Modes

本 skill 支持两种入口,后续校验+补采流程统一：

### 独立运行（standalone）

- **触发**: 未提供 `evidence_snapshot_dir` 和 `environment_backup_dir`
- **行为**: 调用 `scripts/collect_os_evidence.sh --full` 全量采集
- **必须输入**: `--target-pid`、应用类型信息（`--db-type`/`--db-conn`）
- **压测用例**: 采集配置后、执行变更前主动询问用户。有压测用例则走 precise 模式（基线→变更→复测→收益对比→保留/回退）;无则走 quick 模式（只给推荐,标注 verified=false）

### 主SKILL调用（sub-skill）

- **触发**: 主SKILL 提供了 `evidence_snapshot_dir` 和/或 `environment_backup_dir`
- **行为**: 直接读取主SKILL 已采集的证据
- **输入来源**: 主SKILL 任务包

### 统一的证据校验与补采流程

无论证据来自自采集还是主SKILL,获取后都执行相同流程：对照必需证据清单校验完整性 → 有缺失项时调用 `scripts/collect_os_evidence.sh --supplement` 补采 → 补采后仍缺则输出 `required_evidence` 并降级对应参数。

> 独立运行时 os-optimization 完成 Step 0-9 全流程。主SKILL调用时 os-optimization 只做 Step 0-6（分析+输出 candidate_actions）,Step 7-8（应用变更+A/B验证）由主SKILL 执行验证 subagent 完成。

## Inputs

| 输入 | 子SKILL模式 | 独立模式 | 缺失降级 |
|------|-----------|---------|---------|
| `target_pid` | 主SKLL提供 | 必填(或自动检测) | 无法获取 → 跳过 ulimit/process_limits |
| `db_type` | 主SKLL提供 | 必填(或自动检测) | 无法确定 → 通用推荐值,跳过应用特定参数 |
| `db_conn` | 主SKLL提供 | 按应用类型 | MySQL 端口默认 3306;其他需提供 |
| `benchmark_cmd` | 主SKLL提供 | 采集后主动询问 | 无 → quick 模式,标注 verified=false |
| `evidence_snapshot_dir` | 主SKLL提供 | 不需要(自采集) | — |
| `environment_backup_dir` | 主SKLL提供 | 不需要(自采集) | — |
| `bottleneck_classification` | 主SKLL提供 | 可选(用户描述) | 无 → 瓶颈归因降级为"不明确" |
| `agent_action_mode` | 主SKLL统一授权 | 用户确认 | 非 root → 降级 dry-run |
| `tuning_mode` | 主SKLL决定 | 交互确认 | — |
| `output_dir` | 主SKLL指定 | 默认 `./os-optimization-output/` | — |

## Evidence Collection

从 `evidence_snapshot_dir`、`environment_backup_dir` 读取或通过 `scripts/collect_os_evidence.sh` 自采集：

**必需证据**（缺失则对应参数跳过或全量降级）：

- `lscpu` — CPU 平台识别
- `cat /etc/os-release` — OS 版本
- `sysctl -a` 中 `vm.*`、`kernel.numa_balancing`
- `/sys/kernel/mm/transparent_hugepage/{enabled,defrag}` — THP 状态
- `/proc/meminfo` — HugePages、内存
- `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` — governor
- `numactl -H` — NUMA distance 矩阵
- 环境类型检测（物理机/容器/虚拟机）
- `/sys/block/*/queue/{scheduler,nr_requests,rotational}` — I/O scheduler
- `mount` — mount 选项
- `systemctl status irqbalance` — irqbalance 状态
- `cat /proc/cmdline` — GRUB 参数
- `cat /proc/<pid>/limits` — 目标进程 limits

**条件必需**（按应用类型）：

- MySQL: `mysql -e "SHOW VARIABLES"` → `innodb_buffer_pool_size`
- PostgreSQL/openGauss: `psql -c "SHOW shared_buffers"`
- Redis: `redis-cli CONFIG GET maxmemory`
- MongoDB: `mongosh --eval "db.serverStatus().wiredTiger.cache"`

**可选证据**：

- `tuned-adm active` — tuned profile
- `systemctl status atuned` — A-Tune 状态

缺失证据时输出 `required_evidence`，不得直接给出高置信在线变更。详细证据清单和完整性规则见 `references/os-playbook.md` 的 Evidence Requirements 章节。

## Workflow

### Step 0: 证据获取

```
IF evidence_snapshot_dir 或 environment_backup_dir 存在:
  读取已有证据(主SKILL调用入口)
ELSE:
  调用 collect_os_evidence.sh --full 全量采集(独立运行入口)

校验+补采(统一流程):
  对照必需证据清单检查完整性
  IF 有缺失项: 调用 collect_os_evidence.sh --supplement 补采
  生成 os_evidence_manifest.json(含采集时间戳)
  IF 补采后仍有必需证据缺失: 输出 required_evidence,对应参数降级 analysis_only

证据时效性检查:
  IF 采集时间距当前超过 1 小时: 标注 stale,建议重新采集

权限检查:
  IF 当前用户非 root: 所有 execute 降级为 dry-run
```

### Step 1: 前置判断

读取当前参数值 vs 推荐值（推荐值见 `references/os-playbook.md`），已最优则跳过：

- `governor == performance` → 跳过
- `THP == 推荐值`（按应用类型） → 跳过
- `swappiness == 1 或无 swap` → 跳过
- `dirty_ratio == 推荐值`（按存储类型） → 跳过
- `numa_balancing == 0` → 跳过
- `I/O scheduler == none`（NVMe） → 跳过
- `ulimit nofile >= 推荐值` → 跳过
- `mount 已设 noatime` → 跳过

若全部已最优 → 输出 `status=ok, confidence=high`，findings 建议"OS 参数已接近最优,建议转向其他优化方向"，`candidate_actions=[]`。

### Step 2: 平台+NUMA+环境+A-Tune+内核特性+A+K场景识别

- **平台**: 读取 `lscpu`/`/etc/os-release`/`uname -r`，确认鲲鹏型号(916/920/930/950)+ openEuler 版本(22.03=OLK-5.10/24.03=OLK-6.6)。非目标平台降级为 `analysis_only`。
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
- **NUMA 拓扑**: 读取 `numactl -H`，识别 NUMA 节点数/distance/SMT/Node Interleaving（详见 `references/os-playbook.md` NUMA Topology 章节）。
- **环境**: 读取 `environment-type.txt`，判断物理机/容器(标准/特权)/虚拟机。按环境类型确定参数可改性（详见 `references/os-playbook.md` Container Boundary 章节）。
- **A-Tune/tuned 检测**: 检测 `atuned` 服务 → 优先建议 A-Tune；否则检测 `tuned-adm active` → 不输出 tuned 已管理的参数候选动作；两者均无 → 正常输出单项 sysctl。
- **内核特性**: OLK-6.6 检测 EEVDF/mTHP/sched_ext；OLK-5.10 检测 EAS。在 findings 中标注特性影响。

### Step 3: 收益预估+瓶颈归因

- **瓶颈归因**: `bottleneck_classification` 决定 OS 可调参数范围（CPU→governor/irqbalance/numa_balancing；内存→THP/HugePages/swappiness；磁盘IO→I/O scheduler/dirty_ratio/noatime；网络→不主责只采集）。
- **整体收益预估**: 参照 `references/os-playbook.md` Knowledge Base Anchors 的收益表。若整体预估 <1% → 跳过，findings 建议"OS 调优收益有限,建议转向其他 subskill"。

### Step 4: 模式选择（standalone 模式必须主动询问，不可跳过）

> 此步骤在 standalone 模式下必须执行，即使 Step 1 判断参数已最优也不可跳过。
> Agent 必须向用户询问压测用例，根据用户回复确定 tuning_mode。

standalone 模式下，Agent 必须向用户询问是否有压测用例:

> 是否有压测用例用于验证优化效果？
> 有 → 请提供压测命令（如 `sysbench --threads=64 oltp_read_only run`），将执行基线压测 → 变更 → 复测 → 收益对比，根据收益决定保留或回退
> 无 → 将只给出推荐配置，不验证收益（标注 verified=false）

根据用户回复:
- 用户提供压测命令 → `tuning_mode=precise`，记录 `benchmark_cmd`
- 用户无压测命令 → `tuning_mode=quick`，所有动作标注 `verified=false`

```
IF 用户提供了压测命令:
  → tuning_mode=precise
ELSE:
  → tuning_mode=quick
```

用户可显式指定 `tuning_mode`。

### Step 5: 排序+依赖检查+动态计算

- **排序**: 按收益×风险排序（收益表见 `references/os-playbook.md`）。
- **依赖检查**: THP=never → 自动追加 HugePages 检查；停 irqbalance → 检查 IRQ 亲和（独立模式无 cpu-affinity 信号时安全降级，详见 `references/os-playbook.md` Coordination 章节）。
- **动态计算推荐值**: HugePages 数量=`ceil(app_memory/2MB)×1.1` + NUMA 分配；dirty_ratio 按存储类型；nofile 按 max_connections；其他按当前值增量判断（详见 `references/os-playbook.md` 各章节）。

### Step 6: 动作分类 + 容器降级

- 按 `change_mode` 分类: `online` / `restart_required` / `system_reboot`
- 容器环境下需宿主机修改的参数降级为 `analysis_only`，输出到 `container_boundary_notes`
- 虚拟机环境下受 hypervisor 限制的参数降级为 `analysis_only`
- 高风险操作（GRUB/64KB Page Size/mount remount）在 quick 模式下降级为 `analysis_only`

### Step 7: 应用变更

**独立运行**: dry-run → 用户确认 → execute（precise: 逐个 / quick: 分批 3 批）
**主SKILL调用**: 不执行此步，输出 candidate_actions 供主SKILL执行验证 subagent

执行通过 `scripts/apply_optimization_action.sh --action <name>` 调用。支持 `--persist`（持久化）、`--audit-log`（审计日志）、`--dry-run`/`--execute` + `--approved-change-id`。

### Step 8: A/B 验证+收益判定+回退

**独立运行**（precise 模式）: 通过 `scripts/verify_os_change.sh` 执行基线压测→变更→复测→计算 gain_pct→按阈值判定（≥阈值保留 / <阈值询问 / <0 回退+标记）。
**主SKILL调用**: 不执行此步，归主SKILL执行验证 subagent。
**quick 模式**: 跳过验证，所有动作标注 `verified=false`。

### Step 9: 输出

输出 `candidate_actions`/`findings`/`required_evidence`/`fallback_notes` + 持久化文件（rollback 快照、验证结果、审计日志、无效参数标记）。独立运行时同时在对话中给出终端总结。

## Decision Matrix

> 此矩阵为参考基线，实际推荐值由 Step 5 动态计算决定。

| OS Setting | Database OLTP | Compute | RPC | Batch |
|---|---|---|---|---|
| `vm.swappiness` | 1 | 10 | 1 | 10 |
| `vm.dirty_ratio` | 5(NVMe)/3(HDD) | 20 | 5 | 20 |
| `vm.dirty_background_ratio` | 3(NVMe)/1(HDD) | 10 | 3 | 10 |
| `kernel.numa_balancing` | 绑核+绑内存后 0 | 一般场景 1 | 0 | 1 |
| THP | never/madvise | madvise | never | madvise |
| HugePages | PG/openGauss: yes; MySQL: no | test first | no | test first |
| `nofile` / `nproc` | max(conn×2, 65535) | 默认或按进程数 | 65535+ | 默认 |
| irqbalance | 手动 IRQ 亲和后 off | on | off | on |
| CPU governor | performance | performance | performance | performance |
| I/O scheduler NVMe | none | none | none | none |
| I/O scheduler HDD | mq-deadline | mq-deadline | mq-deadline | mq-deadline |
| `vm.overcommit_memory` | Redis: 1 | — | — | — |

## Common Actions

通过 `scripts/apply_optimization_action.sh` 调用：

```bash
# sysctl (online)
apply_optimization_action.sh --action sysctl --key vm.swappiness --value 1 \
  [--persist] [--execute --approved-change-id <id>]

# THP (online)
apply_optimization_action.sh --action thp --value never \
  [--persist] [--execute --approved-change-id <id>]

# governor (online)
apply_optimization_action.sh --action governor --value performance \
  [--persist] [--execute --approved-change-id <id>]

# HugePages (restart_required)
apply_optimization_action.sh --action hugepages --value 84480 [--numa-node 0] \
  [--persist] [--execute --approved-change-id <id>]

# irqbalance (online)
apply_optimization_action.sh --action irqbalance --action stop \
  [--execute --approved-change-id <id>]

# I/O scheduler (online)
apply_optimization_action.sh --action io-scheduler --device sdb --value mq-deadline \
  [--persist] [--execute --approved-change-id <id>]

# GRUB params (system_reboot)
apply_optimization_action.sh --action grub-params \
  --params "sched_steal_node_limit=4 kpti=off mitigation=off" \
  [--execute --approved-change-id <id>]
```

> 默认 dry-run。`--execute` 需配合 `--approved-change-id`。`--persist` 执行后自动追加持久化配置。

## Persistence

每个 candidate_action 含 `persistence_plan` 字段，定义参数持久化方式：

| 参数 | 持久化方式 |
|------|-----------|
| swappiness/dirty_ratio/overcommit | `/etc/sysctl.d/99-os-optimization.conf` |
| THP enabled/defrag | systemd unit 或 `/etc/rc.d/rc.local` |
| governor | `cpupower`（自带持久化） |
| HugePages | `/etc/sysctl.d/99-os-optimization.conf` |
| I/O scheduler | udev rule (`/etc/udev/rules.d/60-io-scheduler.rules`) |
| noatime mount | `/etc/fstab` |
| irqbalance disable | `systemctl disable`（自带持久化） |

执行策略: quick 模式自动追加持久化（除非 `--no-persist`）；precise 模式 A/B 验证通过后才持久化。

## Dependencies

| 工具 | 用途 | 必需性 | 缺失影响 |
|------|------|--------|---------|
| `lscpu` | CPU 平台识别 | 必需 | 全量降级 |
| `sysctl` | 内核参数采集 | 必需 | 全量降级 |
| `numactl` | NUMA 拓扑 | HugePages NUMA 分配必需 | HugePages 降级为全局分配 |
| `systemctl` | irqbalance/tuned/A-Tune | 对应参数必需 | 跳过对应参数 |
| `tuned-adm` | tuned profile | 可选 | 跳过 tuned 检查 |
| `mount` | mount 选项 | noatime 必需 | 跳过 noatime |
| `mysql`/`psql`/`redis-cli`/`mongosh` | 数据库配置 | 对应应用场景必需 | HugePages 降级为估算 |
| `sysbench`/`pgbench`/`redis-benchmark` | A/B 验证 | precise 模式必需 | 降级为 quick |
| `cpupower` | governor 设置 | 可选 | 用 echo /sys 替代 |
| `grub2-mkconfig` | GRUB 参数 | GRUB 参数必需 | 跳过 GRUB 参数 |

## Platform Notes

| 型号 | governor | THP | HugePages | numa_balancing | SMT |
|------|----------|-----|-----------|---------------|-----|
| 916 (0xd01) | 固定 2.4GHz,收益 <5% | 标准大页 | 标准 | 标准 | 无 |
| 920 (0xd01) | 固定 2.6GHz,收益 <5%(防C-state) | L2 TLB +11c,收益高 | L2 TLB,收益高 | 跨SCCL无L3共享,关闭收益高 | 无 |
| 930 (0xd03) | 2.2GHz,待确认 | TLB改进待确认 | 待确认 | SMT2,需实测 | SMT2 |
| 950 (0xd06) | 动态1.2-2.3GHz,**有实际意义** | 待确认 | 待确认 | 192核,开销大 | SMT2 |

| openEuler | 内核 | 调度器 | THP | 特殊注意 |
|---------|------|--------|-----|---------|
| 22.03 | OLK-5.10 | CFS+EAS | 标准 THP | EAS 下 governor 策略建议验证 |
| 24.03 | OLK-6.6 | EEVDF | mTHP | tuned sched 参数可能不准确；sched_ext 可用 |

## Reject Conditions

候选出现以下任一情况应跳过或降级：

- 参数已为推荐值（Step 1 前置判断跳过）
- 容器内且参数需宿主机改（降级 `analysis_only`）
- 虚拟机且参数受 hypervisor 限制（降级 `analysis_only`）
- 非 root 用户（所有 execute 降级 dry-run）
- OS 调优整体预估收益 <1%（跳过，建议转向其他 subskill）
- 瓶颈归因为网络（OS 不主责，只采集不输出动作）
- quick 模式下高风险操作（GRUB/64KB Page Size/mount remount 降级 `analysis_only`）
- 鲲鹏 930/950 待确认参数（标注 low confidence）

## Change Classification

| change_mode | 示例 | 风险 |
|---|---|---|
| `online` | sysctl、governor、THP、I/O scheduler、irqbalance、sched-feature | Low |
| `restart_required` | HugePages、ulimit、tuned | Medium |
| `system_reboot` | GRUB 参数（kpti/mitigation/sched_steal） | High |
| `analysis_only` | 容器/权限不足/用户未批准/quick模式高风险 | Low |

当环境限制了动作时，将该动作保留在 findings 中，但不进入可执行 `candidate_actions`。

## Interaction Notes

- `cpu-affinity-optimization` 已手动管理 IRQ 时，需要关闭 irqbalance（os 先停 → cpu-affinity 设 IRQ 亲和 → cpu-affinity 绑核）。
- 绑核+绑内存后建议关闭 `kernel.numa_balancing`（独立判断,非时序依赖）。
- 网络参数（`net.core.*`/`net.ipv4.*`/`net.core.netdev_budget`）由 `network-optimization` 主责；本 skill 只采集不输出动作。
- 容器环境下不得承诺宿主机 OS 参数已经修改成功。
- HugePages 分配后需重启应用使其使用大页。
- 独立模式下无 cpu-affinity 信号时，irqbalance 不输出候选（安全降级），numa_balancing 仅基于 numactl distance 判断。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含以下字段（符合 `subagent-orchestration.md` 契约）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | string | 唯一标识 |
| `title` | string | 简短标题 |
| `category` | string | 动作分类（`sysctl`/`thp`/`hugepages`/`governor`/`irqbalance`/`io-scheduler`/`ulimit`/`tuned`/`grub-params`/`sched-feature`/`mount`） |
| `priority` | string | `high` / `medium` / `low` |
| `change_mode` | string | `analysis_only` / `dry_run` / `online` / `restart_required` / `system_reboot` |
| `requires_root` | boolean | 是否需要 root 权限 |
| `risk` | string | `low` / `medium` / `high` |
| `implementation_plan` | string | 实施计划，含具体命令（dry-run 和 execute） |
| `validation_plan` | string | 验证方法（如何确认参数已生效） |
| `rollback` | string | 回退方法，含恢复原值的完整命令 |
| `expected_effect` | string | 预期效果描述 |
| `expected_gain_metric` | object | 指标与预期收益（如 `{"metric": "tps", "expected_gain_pct": "0-2%"}`） |
| `rejection_criteria` | string[] | 不采纳条件（如 `["gain_pct < 0.3"]`） |
| `evidence_refs` | string[] | 证据路径 |

`rollback` 必须包含恢复原值的完整命令；HugePages 动作的 rollback 必须先释放再恢复原值。危险动作（写 `/proc/sys`、写 `/sys/kernel/mm/transparent_hugepage/*`、修改 `scaling_governor`、停止 `irqbalance`、修改 `/sys/block/*/queue/scheduler`、mount remount、GRUB 修改）不得在分析阶段执行。详细安全门禁见 `references/os-playbook.md`。

## Outputs

必须输出以下顶层字段（符合 subagent-orchestration.md 契约）：

- `subskill_name` — 必须为 `os-optimization`
- `current_run_id` — 必须与任务包一致
- `current_evidence_status` — 必须为 `current` 才能输出候选动作
- `status` — `ok` / `degraded` / `blocked` / `failed`
- `confidence` — `high` / `medium` / `low`
- `analysis_timestamp` — ISO 8601
- `evidence_sources` — 引用的证据路径数组
- `findings` — 结构化发现，包含以下子字段：
  - `os_findings` — OS 配置分析结果
  - `online_change_actions` — 可在线变更的动作清单
  - `restart_required_actions` — 需服务重启的动作清单
  - `system_reboot_actions` — 需系统重启的动作清单
  - `container_boundary_notes` — 容器边界说明（容器内可改/不可改/需宿主机协助）
- `candidate_actions` — 候选动作数组（结构见 Candidate Action Contract）
- `required_evidence` — 缺失证据（无缺失时为空数组）
- `fallback_notes` — 降级说明（无降级时为空数组）
- `timing` — 耗时统计，至少包含 `analysis_seconds`

若证据不足，输出 `status=degraded|blocked`，并列出最小补采命令；不要把缺失证据解释成"OS 无瓶颈"。

## Boundary

- BIOS Power Profile、SMT、C-State、NUMA/Node Interleaving、DDR Speed 等固件/BMC 配置由 `bios-optimization` 负责。
- 网卡队列/RSS/RPS/XPS/TCP 协议栈参数由 `network-optimization` 负责。
- 线程绑核/NUMA 绑定/cpuset 由 `cpu-affinity-optimization` 负责。
- 应用参数（线程数/连接池/buffer pool）由 `application-config-optimization` 负责。
- 编译选项/LTO/PGO 由 `compiler-optimization` 负责。

## NPU 推理 cgroup 设备控制

针对 Ascend NPU 推理场景，OS 层需额外管理 `/dev/davinci*` 设备访问权限与多租户隔离。本章节适用于 A+K 场景识别命中（Step 2 第 2 步检测到 NPU）后的补充调优。

### 背景

昇腾 NPU 通过字符设备暴露给用户态：

| 设备路径 | 用途 |
|---------|------|
| `/dev/davinci0` ... `/dev/davinciN` | NPU 计算设备（每张卡一个） |
| `/dev/davinci_manager` | 设备管理通道 |
| `/dev/devmm_svm` | 设备内存管理（SVM 共享虚拟内存） |
| `/dev/hisi_hdc` | 海思 HCCN 通信通道（HCCL 依赖） |

应用进程（vLLM EngineCore、torch_npu init）需对这些设备有 `rw` 权限，否则 `aclrtSetDevice` 报权限错误。

### cgroup v2 device controller

cgroup v2 通过 `cgroup.bpf` + `bpf` 程序实现设备过滤，或回退到 `cgroup v1` 的 `devices` 子系统。openEuler 22.03/24.03 默认 cgroup v2 hybrid。

**v2 写法（推荐，需 kernel 5.10+）**：

```bash
# 创建 NPU 推理专用 cgroup
mkdir -p /sys/fs/cgroup/npu_inference
echo "+device" > /sys/fs/cgroup/npu_inference/cgroup.subtree_control

# 允许访问 davinci0（白名单策略需 BPF，简化为权限降级）
chmod 660 /dev/davinci0 /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc
chown root:hw_davinci /dev/davinci0 /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc
usermod -aG hw_davinci <app_user>
```

**v1 写法（兼容老环境）**：

```bash
mkdir -p /sys/fs/cgroup/devices/npu_inference
echo "deny" > /sys/fs/cgroup/devices/npu_inference/devices.deny
echo "c 1:5 rwm" > /sys/fs/cgroup/devices/npu_inference/devices.allow   # /dev/zero 示例
# davinci0 主次设备号通过 `ls -l /dev/davinci0` 获取，假设为 242:0
echo "c 242:0 rwm" > /sys/fs/cgroup/devices/npu_inference/devices.allow
```

### 验证命令

```bash
# 检查设备权限
ls -l /dev/davinci* /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc

# 检查进程是否在目标 cgroup
cat /proc/<pid>/cgroup
```

### 实战结论

- 实战（Ascend910 + vLLM qwen2.5-1.5b 单租户场景）未触发 cgroup 隔离，仅做权限校验
- 多租户部署此章节为**必需**，单租户可降级为权限检查
- 容器场景下若 `--device` 未映射全 4 个设备，torch_npu 初始化失败报 `aclrtSetDevice` 错误

## HugePages 与 vLLM KV Cache

vLLM 使用 PagedAttention，KV cache 以 page（块）形式分配在 host RAM 或 device HBM。本章节聚焦 host 侧 KV cache 与模型权重加载场景的大页优化。

### vLLM KV Cache 内存模型

```
[GPU/NPU HBM]   ← PagedAttention KV blocks（device 内存，不在此讨论）
[Host RAM]      ← 模型权重、CUDA/npu host buffer、swap fallback
```

vLLM 在 host 侧分配场景：
- 模型权重加载（`safetensors` → host RAM → device HBM）
- AsyncEngine 多进程 IPC 共享内存
- CPU offload 场景（vLLM cpu+gpu 混合）

### 大页配置方法

**静态预留 HugePages**（推荐，确定性强）：

```bash
# 计算所需大页数：app_memory_GB × 1024 / 2MB × 1.1（10% 余量）
# 例如 32GB 模型 → 32×1024/2×1.1 = 18176 pages
echo 18176 > /proc/sys/vm/nr_hugepages

# NUMA 分配（鲲鹏双 NUMA 节点，按需分配到指定节点）
echo 9088 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
echo 9088 > /sys/devices/system/node/node1/hugepages/hugepages-2048kB/nr_hugepages

# 持久化
echo "vm.nr_hugepages = 18176" >> /etc/sysctl.d/99-os-optimization.conf
```

**GLIBC_TUNABLES 大页（glibc >= 2.34）**：

```bash
# glibc malloc 申请大页（无需应用改造）
export GLIBC_TUNABLES=glibc.malloc.tcache_count=0:glibc.malloc.hugetlb=1

# 应用启动
GLIBC_TUNABLES=glibc.malloc.tcache_count=0:glibc.malloc.hugetlb=1 \
  python -m vllm.entrypoints.openai.api_server --model qwen2.5-1.5b ...
```

| 参数 | 作用 | NPU 推理推荐值 |
|------|------|--------------|
| `glibc.malloc.tcache_count` | tcache 缓冲数，0 关闭 | `0`（避免跨线程缓存污染） |
| `glibc.malloc.hugetlb` | malloc 走大页 | `1`（启用） |
| `glibc.malloc.mmap_max` | mmap 阈值 | 默认，vLLM 大块分配走 mmap |
| `vm.nr_hugepages` | 系统大页池 | 按模型大小计算 |

### PyTorch / torch_npu malloc 行为

- PyTorch 默认 allocator 不走 glibc malloc（自己管理 CUDA/npu caching allocator）
- torch_npu HBM 分配与 host RAM 解耦，大页优化主要影响**权重加载阶段**与**host buffer**
- vLLM 的 `weight_loader` 使用 `np.memmap` + `torch.from_numpy`，权重加载阶段会触发大页映射

### tcmalloc 拦截陷阱（实战）

> ⚠️ **关键陷阱**：若应用通过 `LD_PRELOAD` 加载 `libtcmalloc.so`，则所有 `malloc` 调用被 tcmalloc 拦截，glibc 大页调优（GLIBC_TUNABLES）**完全失效**。

实战 Ascend910 + vLLM 场景：
- vLLM 启动脚本设置 `LD_PRELOAD=libtcmalloc.so`
- 设置 `GLIBC_TUNABLES=glibc.malloc.hugetlb=1` 无效果
- `cat /proc/meminfo | grep HugePages` 显示已分配但使用率 0%

**验证 tcmalloc 是否生效**：

```bash
# 检查进程是否加载 tcmalloc
cat /proc/<pid>/maps | grep -i tcmalloc

# 若有匹配行，则 glibc 大页调优失效，需直接关闭 tcmalloc 或改用 tcmalloc 自带大页（需重新编译）
```

**绕过方法**：
1. 移除 `LD_PRELOAD`（若 tcmalloc 不是性能瓶颈）
2. 改用 `jemalloc` + 大页（jemalloc 支持 `MALLOC_CONF` 大页）
3. 编译 tcmalloc with `--enable-hugetlbfs`（需重新编译 gperftools）

### 实战结论

| 模型规模 | 大页收益 | 原因 |
|---------|---------|------|
| < 2B（如 qwen2.5-1.5b） | **无收益** | 内存压力小，TLB miss 不是瓶颈；权重加载阶段短暂，大页节省的 TLB miss 被其他开销淹没 |
| 7B - 13B | **可能有收益** | 权重 14-26GB，TLB miss 在加载阶段显著；需 tcmalloc 不拦截 |
| > 30B | **可能有收益** | 权重 >60GB，TLB miss 主导加载阶段；强烈建议大页 |

实战平台（Ascend910 + qwen2.5-1.5b）：
- 大页配置被拒绝（tcmalloc 拦截 + 小模型无收益）
- 总优化收益 +94.5% 来自其他维度（绑核、并发等），大页贡献 0%

> 通用场景下大页仍是**重要候选**，不应因单场景拒绝而全盘否定。本 skill 在 Step 5 排序时按模型大小动态评估收益。

## 调度器对 AI 推理线程的影响

Linux 调度器演进对 vLLM AsyncEngine 等异步推理框架有直接影响。本章节评估调度器参数与可替代调度器在 NPU 推理场景的潜力。

### 调度器版本基线

| 内核 | 默认调度器 | 关键特性 | openEuler 版本 |
|------|----------|---------|--------------|
| OLK-5.10 | CFS + EAS（鲲鹏） | 时间片公平，EAS 选能效核 | 22.03 |
| OLK-6.6 | EEVDF | 按截止时间排序，延迟敏感优化 | 24.03 |
| 主线 6.12+ | EEVDF + sched_ext | 可加载 BPF 调度器 | — |

> **实战平台（OLK-5.10）仍用 CFS**，本 skill 输出 CFS 调优参数；升级到 OLK-6.6 后需重新评估 EEVDF 影响。

### vLLM AsyncEngine 调度敏感点

vLLM AsyncEngine 使用 asyncio event loop：

```
[Main event loop] → [request queue] → [EngineCore worker] → [NPU 算子下发]
       ↑ asyncio.sleep / await                          ↓ 同步等待 NPU 完成
[Token streaming] ←─────────────────────── [result queue] ←
```

调度延迟敏感点：
1. **event loop 唤醒延迟**：await 后被唤醒的延迟，CFS 默认 `sched_latency_ns` 6ms 可能过大
2. **EngineCore worker 被抢占**：算子下发途中被其他线程抢占，造成 NPU 空等
3. **wakeup 抢占**：高优先级线程唤醒时是否立即抢占当前线程

### CFS 调优参数（OLK-5.10）

```bash
# 当前值
sysctl kernel.sched_latency_ns
sysctl kernel.sched_min_granularity_ns
sysctl kernel.sched_wakeup_granularity_ns
sysctl kernel.sched_wakeup_preempt_ns
```

| 参数 | 默认值 | NPU 推理推荐 | 说明 |
|------|--------|------------|------|
| `sched_latency_ns` | 6000000（6ms） | 2000000-3000000（2-3ms） | 调度周期，降低可减小唤醒延迟 |
| `sched_min_granularity_ns` | 2000000（2ms） | 1000000（1ms） | 单线程最小运行时间 |
| `sched_wakeup_granularity_ns` | 2000000（2ms） | 500000-1000000（0.5-1ms） | 唤醒抢占粒度，降低可加速唤醒 |
| `sched_wakeup_preempt_ns` | - | 同 wakeup_granularity | 唤醒抢占阈值 |

```bash
# 应用（在线，需 root）
sysctl -w kernel.sched_latency_ns=3000000
sysctl -w kernel.sched_min_granularity_ns=1000000
sysctl -w kernel.sched_wakeup_granularity_ns=1000000

# 持久化
cat >> /etc/sysctl.d/99-os-optimization.conf <<EOF
kernel.sched_latency_ns = 3000000
kernel.sched_min_granularity_ns = 1000000
kernel.sched_wakeup_granularity_ns = 1000000
EOF
```

> ⚠️ 这些参数是**全局**的，会影响所有线程。若系统上有其他延迟不敏感负载（如 batch 作业），降低 sched_latency 可能损害其吞吐。建议仅在**推理专用机**上调整。

### EEVDF 影响（OLK-6.6 / kernel 6.6+）

EEVDF（Earliest Eligible Virtual Deadline First）替代 CFS：
- 按虚拟截止时间排序，延迟敏感任务优先
- 引入 `sched_feat` `LATENCY_WARN` 监控延迟抖动
- 默认参数更激进，asyncio 唤醒延迟预期更优

**调优方向**（OLK-6.6 需实测）：
- `kernel.sched_base_slice_ns` 替代 `sched_min_granularity_ns`
- `kernel.sched_features` 中 `EEVDF` 相关位
- 暂无 vLLM on NPU 在 EEVDF 下的实测数据，标注 `verified=false`

### sched_ext 潜力

`sched_ext` 允许通过 BPF 加载自定义调度器：
- 可针对 vLLM 推理场景编写专用调度策略（如 EngineCore 优先级最高）
- 实战平台 OLK-5.10 不支持，OLK-6.6 实验性支持
- 暂无生产级 NPU 推理 sched_ext 调度器，标注 `experimental`

### 验证方法

```bash
# 测量唤醒延迟分布（需 perf）
perf sched record -p <vllm_pid> -- sleep 10
perf sched latency -p <vllm_pid>

# 监控调度延迟告警（EEVDF）
echo 1 > /sys/kernel/debug/sched/latency_warn_enabled
dmesg | grep "sched:.*latency"

# 监控抢占次数
perf stat -e context-switches,cpu-migrations,sched:sched_switch -p <pid> -- sleep 5
```

### 实战结论

| 平台 | 调度器调优 | 实测收益 |
|------|----------|---------|
| OLK-5.10（实战） | CFS 参数降低 | 未单独测量，被绑核收益掩盖 |
| OLK-6.6 | EEVDF 默认参数可能已足够 | 未实测，标注 verified=false |
| 主线 6.12+ | sched_ext 实验 | 实验性，不推荐生产 |

> 调度器参数调优是**次要候选**，仅在绑核 + governor 之后仍存在调度抖动时考虑。
