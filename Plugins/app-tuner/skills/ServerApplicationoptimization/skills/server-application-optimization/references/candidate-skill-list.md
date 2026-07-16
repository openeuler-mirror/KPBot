# 候选优化 Skill 列表生成规则

本文定义“根据采集信息生成候选优化 skill 列表”的规则。主 agent 必须先完成本轮性能信息采集，再生成 `candidate_skill_list`；该列表不是静态路由表，而是本轮证据驱动的优化执行顺序。

## 候选列表格式

`candidate_skill_list` 是数组，按执行顺序排列。先执行证据命中的候选 skill，再执行未命中的覆盖 skill，确保最终所有主优化 skill 都被分析或明确跳过原因。

```json
[
  {
    "candidate_id": "candidate-skill-001",
    "phase": "evidence_candidate",
    "subskill_name": "performance-library-selection",
    "priority": "high",
    "reason": "hotspot DSO shows libssl.so at 12.4%",
    "source_signal": "third_party_library_hotspot",
    "required_evidence": ["evidence_snapshot_dir/performance_signal_summary.json"],
    "stop_rule": "stop this subskill after 5 rounds with gain < 1%"
  }
]
```

兼容字段：历史工具可能仍读取 `dynamic_route_plan`。新实现必须同时写入 `candidate_skill_list`，并把 `dynamic_route_plan` 作为等价兼容视图，不再作为主语义。

## 性能信息采集要求

基线确认后必须尽量采集以下信息，并写入 `performance_signal_summary.json`：

- 按进程采集火焰图或 perf call stack 数据。
- 按热点函数、DSO/so、进程维度排序，生成 `hotspot_function_rank` 和 `hotspot_dso_rank`。
- 采集 topdown 或 perf stat 信息，至少覆盖 L1 icache miss、L3/LLC miss、context-switches。
- 采集线程分布、线程迁移、上下文切换、CPU/NUMA/IRQ 关联信息。

工具缺失或权限不足时，必须在 `degraded_capabilities` 说明缺失项；不得把缺失证据解释成“无瓶颈”。

## 采集信号到候选 Skill 的规则

| 采集信号 | 判定方式 | 加入候选 skill |
|---|---|---|
| 高热点第三方库 | `hotspot_dso_rank` 中第三方 `.so` 占比达到阈值，或热点函数落在 malloc/memcpy/压缩/加密/CRC 等外部库 | `performance-library-selection` |
| 网络相关热点高 | `hotspot_function_rank` 中 TCP/UDP/socket/epoll/softirq/NAPI/SKB/netfilter/NIC driver 等符号占比达到阈值 | `network-optimization` |
| L1 icache miss 高 | topdown 或 perf stat 中 L1 icache miss 比例达到阈值 | `compiler-optimization`，重点分析 PGO/LTO、布局、内联、代码尺寸和目标架构选项 |
| 线程切换高且 L3/LLC cache miss 高 | `context_switch_rate_per_sec` 高，且 L3/LLC miss 比例达到阈值 | `cpu-affinity-optimization`，重点分析绑核、NUMA、线程迁移和 IRQ 隔离 |
| 数据库/应用内部状态解释瓶颈 | 连接池、队列、buffer/cache、锁等待、数据库状态可解释瓶颈 | `application-config-optimization` |
| OS/BIOS/硬件/加速卡证据命中 | 环境诊断或采集信息显示对应问题 | `os-optimization`、`bios-optimization`、`accelerator-optimization`、`hardware-upgrade-analysis` |
| 既有分类无法覆盖 | 证据明确存在异常但不属于现有专项 | `other-optimization` |

默认阈值可由脚本参数覆盖：

- 第三方库热点：单个 DSO 或聚合占比 `>= 5%`。
- 网络热点：网络相关符号聚合占比 `>= 3%`。
- L1 icache miss：`L1-icache-load-misses / L1-icache-loads >= 5%`，或 topdown 前端/取指指标明确为 high。
- L3/LLC miss：`LLC-load-misses / LLC-loads >= 5%`。
- 上下文切换：`context_switch_rate_per_sec >= 1000`，或 pidstat 显示线程切换异常集中。

阈值只是候选生成的默认启发；subskill 仍必须基于证据输出候选动作、验证方法、风险和回退方法。

## cpu-affinity-optimization 强制规则

`cpu-affinity-optimization` 是唯一的强制候选 skill，不受证据信号阈值约束：

- **始终执行**：无论 `context_switch_rate_per_sec` 或 `LLC-load-misses / LLC-loads` 是否达到阈值，`cpu-affinity-optimization` 都必须加入 `candidate_skill_list`。
- **第一优先级**：在 `candidate_skill_list` 中始终排在 `priority=highest` 的首位，在所有其他候选 skill 之前执行。
- **执行完成门控**：cpu-affinity-optimization 必须执行完成（状态为 `completed` 或 `stopped`）后，其他候选 skill 才能开始执行。在 cpu-affinity-optimization 的 `per_skill_iteration_state.status` 未标记为完成状态前，禁止启动任何其他 skill 的分析 subagent 或执行验证 subagent。
- **违规检测**：若其他 skill 在 cpu-affinity 完成前已被执行，相关收益在 `per_skill_gain_summary` 中必须标记为 `confounded`，且 `skill_execution_order.cpu_affinity_first_verified=false`。
- **理由分类**：
  - 当信号命中（`context_switch_high=true` 且 `l3_cache_miss_high=true`）→ `source_signal=context_switch_and_llc_miss_high`
  - 当信号未全部命中 → `source_signal=mandatory_baseline_check`，`reason=CPU 亲和性是所有服务器应用的基础优化，必须作为基线检查`
- 该 skill 不受 coverage 阶段追加规则影响：已在候选列表中的 skill 不会重复加入 coverage。

### cpu-affinity 完整检查清单（6 项，缺一不可）

执行 cpu-affinity-optimization 时，**必须按以下顺序逐项检查**。`Step 0` 是**物理拓扑发现**，必须在任何 IRQ/进程调整之前完成。

#### Step 0：物理拓扑发现（必须最先执行）

```bash
# 1. 找出所有活跃网络设备的 PCI 地址和物理 NUMA 节点
for dev in $(ls /sys/class/net/); do
    carrier=$(cat /sys/class/net/$dev/carrier 2>/dev/null)
    [ "$carrier" = "1" ] || continue
    node=$(cat /sys/class/net/$dev/device/numa_node 2>/dev/null)
    echo "$dev: NUMA Node $node"
done

# 2. 找出 NVMe/磁盘控制器的物理 NUMA 节点
cat /sys/block/nvme*/device/numa_node 2>/dev/null

# 3. 列出所有 NUMA 节点的 CPU 范围和内存
for n in $(ls -d /sys/devices/system/node/node* 2>/dev/null); do
    echo "$(basename $n): CPUs=$(cat $n/cpulist), Mem=$(cat $n/meminfo | grep MemTotal)"
done
```

**物理拓扑决定一切**：网卡 IRQ 必须绑在网卡所在的 NUMA 节点上，应用进程应绑定在与关键设备相同的 NUMA 节点。

#### 亲和性决策树

```
Step 0 产出的物理拓扑:
  NIC_NODE   = <活跃网卡物理 NUMA 节点>
  DISK_NODE  = <主存储设备物理 NUMA 节点>
  APP_CORES  = <目标进程当前绑核范围>

决策逻辑:
  if NIC_NODE == DISK_NODE:
      TARGET_NODE = NIC_NODE  # 网卡和存储同节点，无争议，直接选定
  else:
      # ⚠️ 网卡和存储在不同节点 → 禁止理论推测，必须实测
      device_numa_conflict = true
      
      # 方案 A：绑 DISK_NODE (NVMe 同节点)
      # 方案 B：绑 NIC_NODE (网卡同节点)
      
      # 两个方案各跑完整 benchmark (≥120s, 同预热, 同并发)
      # 对比 TPS/P95 实测数据 → 取最优方案
      # 差异 < 2% → 选用内存更大的节点
      
      # ⚠️ 禁止跳过实测直接输出建议！
      # 真实案例：Kunpeng 920, BP hit rate 95.6%
      #   理论 → BP 命中率高 → 绑 NVMe 侧
      #   实测 → NIC 侧 TPS +4.3%, P95 -19.4% 
      #   理论预测错误！网络 DMA 跨 socket 代价 >> NVMe 跨 socket 代价

正确布局:
  目标进程:          TARGET_NODE 专用 CPU（实测选定的 node）
  网卡 IRQ:          NIC_NODE CPU（与网卡同节点，不与目标进程重叠）
  存储 IRQ:          DISK_NODE CPU（与存储同节点）
  irqbalance:        STOP（手动设置后必须停止，防止自动回退）
```

#### 检查清单

| # | 检查项 | 命令/方法 | 判定标准 |
|---|--------|-----------|----------|
| 0 | **物理拓扑发现** | `/sys/class/net/*/device/numa_node` + `/sys/block/*/device/numa_node` | 明确每个活跃设备的物理 NUMA 节点，**必须在任何调整前执行** |
| 1 | **NUMA 内存本地化** | `numastat -p <pid>` | 目标进程 >95% 内存在目标 NUMA 节点 |
| 2 | **应用 CPU 绑核** | `numactl --cpunodebind=N --membind=N` 或 cgroup cpuset | 应用进程绑定在 TARGET_NODE 的专用核心上 |
| 3 | **网卡 IRQ NUMA 对齐** | `/proc/irq/<n>/effective_affinity_list` | 网卡数据队列 IRQ 的 effective_affinity **必须落在 NIC_NODE** 的 CPU 上（不是"不在应用核心上就行"，而是"必须和网卡同节点"） |
| 4 | **NVMe IRQ 对齐** | `/proc/interrupts` 中 nvme 条目 | NVMe IRQ 落在 NVME_NODE 的 CPU 上；内核托管中断无法手动迁移时记录为 `degraded` |
| 5 | **irqbalance 状态** | `systemctl stop irqbalance` | 手动设置 IRQ 后必须停止 irqbalance，或配置 banned_cpus |
| 6 | **设备 NUMA 冲突实测** | 分别部署在两个候选 NUMA node 上，各执行 ≥120s benchmark | 当 NIC_NODE ≠ DISK_NODE 时**必须实测**（禁止理论推测）。对比 TPS/P95，取最优方案。差异 <2% 时选用内存更大的 node。|

**⚠️ 常见遗漏**：
1. 只检查"设备 IRQ 不在目标进程核心上"而**不查设备的物理 NUMA 节点** → IRQ 可能被绑在与设备不同 NUMA 节点的 CPU 上，网络 DMA 跨节点开销可达 100%+ 延迟增加。
2. 不先查 `/sys/class/net/<dev>/device/numa_node` 和 `/sys/block/<dev>/device/numa_node` → 不知道设备物理位置就做亲和性优化是盲目的。
3. 先在 `/proc/interrupts` 看 IRQ 分布再决定绑定目标，而不是先在 sysfs 查物理拓扑 → 正确顺序必须是"物理拓扑 → 决策 TARGET_NODE → 调整 IRQ/进程"。
4. **NIC 和 NVMe 不同 NUMA node 时，仅凭理论（workload 类型、BP 命中率）做选择，不执行实测 A/B benchmark** → 理论预测可能完全错误。网卡与 NVMe 跨 socket 的 DMA 延迟不对称性无法从 sysfs 推断，必须实测。真实案例：BP 命中率 95.6% 的只读 MySQL，理论应绑 NVMe 侧，但实测 NIC 侧 TPS +4.3%。

## 执行顺序

1. **证据候选阶段**：按 `candidate_skill_list.phase=evidence_candidate` 顺序执行。**`cpu-affinity-optimization` 始终优先执行。** 每个 skill 必须由独立分析 subagent 只读分析，并在 `subagent_invocation_log` 中记录任务 ID、任务包和结果 JSON；随后进入串行执行验证轮次。
2. **覆盖执行阶段**：证据候选全部完成后，把未进入证据候选列表的主优化 skill 追加为 `phase=coverage`，逐个启动分析 subagent 并做必要验证，确保最终所有主优化 skill 都有结论。
3. **报告阶段**：最终报告必须区分证据命中候选、覆盖执行 skill、未产生动作的 skill、被阻塞 skill 和停止原因。
4. **执行顺序硬约束**：`candidate_skill_list` 的执行顺序是硬约束，不允许在 cpu-affinity-optimization 完成前跳到后续 skill。主 agent 必须在每次进入迭代轮次前校验当前 skill 是否为 `candidate_skill_list` 中第一个未完成的 skill，且 cpu-affinity-optimization 必须是第一个被执行的 skill。

主优化 skill 覆盖集合：

- `application-config-optimization`
- `performance-library-selection`
- `cpu-affinity-optimization`
- `network-optimization`
- `compiler-optimization`
- `os-optimization`
- `bios-optimization`
- `accelerator-optimization`
- `hardware-upgrade-analysis`
- `other-optimization`

`io-memory-network-bottleneck-analysis` 是前置瓶颈预筛，不属于覆盖执行集合。`database-workload-analysis` 默认由 `application-config-optimization` 按需触发；当 `--database-workload` 明确给出时，可追加到覆盖集合。

## 停止规则

- 单个 skill 最多尝试 5 轮。
- 同一 skill 5 轮阶段收益均 `< 1%` 时停止该 skill。
- 停止单个 skill 不等于全局停止；必须继续执行 `candidate_skill_list` 中尚未完成的 skill，包括 coverage 阶段 skill。
- 所有主优化 skill 均完成、停止、阻塞并已说明原因后，才能进入最终报告、review、还原和归档。

## 安全门禁

- 候选列表生成阶段只生成 `candidate_skill_list` 和任务包，不实施变更。
- 子 skill 分析阶段只能由分析 subagent 读取证据并输出候选动作，不得修改系统。
- 真实变更只允许在串行执行验证 subagent 阶段发生，且必须满足 `approved_execute`、目标实例身份、证据新鲜度、回退计划和风险门禁。
- 基线确认后不得逐个 skill 请求用户批准；执行验证 subagent 只校验已确认授权边界，超出边界时阻塞并交回主 agent 统一确认。
