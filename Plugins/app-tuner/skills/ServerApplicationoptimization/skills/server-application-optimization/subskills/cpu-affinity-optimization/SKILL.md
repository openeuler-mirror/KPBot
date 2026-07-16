---
name: cpu-affinity-optimization
description: 在确认瓶颈主要位于 CPU 侧后，基于线程、NUMA 和中断分布进行绑核、绑内存与中断亲和性优化，作为 server-application-optimization 的子 skill 使用。
---

# CPU Affinity Optimization

当主瓶颈已经落到 CPU 侧后，优先使用本子 skill。

本子 skill 是统一入口：

- 通用 CPU 亲和性分析入口
- 根目录 `ref-skills/cpu-affinity-optimization` 的适配层
- 平台判断、依赖检查和回退决策中心

## Recommended Inputs

- `target_pid` — 目标进程 PID
- `thread_cpu_snapshot` — 线程与 CPU 分布快照
- `architecture` — CPU 架构
- 当前 NUMA 拓扑（核数、节点数、NUMA distances）
- **活跃 I/O 设备的 NUMA node**（网卡、NVMe、GPU/NPU 的 `/sys/.../device/numa_node`）
- **workload 类型**（`remote_benchmark` / `local_benchmark` / `io_intensive` / `compute` / `mixed` / `multi_tenant`）
- **网络流量方向**（是否远程压测？活跃网卡是哪张？流量多大？）
- 当前环境类型（`baremetal` / `vm` / `container`）
- 容器 cpuset 范围（容器环境时）
- `change_scope` — 用户允许的变更范围
- `restart_allowed` — 是否允许重启

重点关注：

- **设备-NUMA 拓扑对齐（强制首步）**：网卡、NVMe、GPU/NPU 等 PCIe 设备物理连接在哪个 NUMA node/socket
- **Workload 感知的亲和性优先级**：远程压测 → 网卡对齐优先；本地 IO 密集型 → 存储对齐优先；纯计算 → 内存带宽对齐优先
- 应用绑核
- 中断绑核
- NUMA 亲和性
- 线程布局
- 内存绑定
- 多插槽 NUMA 拓扑
- 跨 NUMA 节点访问代价
- `numactl --preferred` 与 `taskset` 的组合使用和优先级
- 容器 cpuset 限制
- 容器环境下 IRQ 亲和性与跨 NUMA 隔离限制
- 线程与 CPU 之间的调度均衡性
- 热点核与线程分布偏斜

## Repo-local Integration

当前支持按条件优先接入仓库内置 subskill：

- 默认优先路径：`ref-skills/cpu-affinity-optimization`
- 入口文件：`ref-skills/cpu-affinity-optimization/SKILL.md`
- 默认脚本目录：`ref-skills/cpu-affinity-optimization/scripts`

仓库内置 CPU 亲和性 subskill 提供：

- 系统拓扑与 NUMA 诊断
- 线程/进程当前亲和性分析
- 线程 CPU 分布与迁移分析
- IRQ 与业务线程冲突分析
- CPU 亲和性策略生成
- 验证与回滚脚本骨架

如果仓库内置 subskill 缺失或依赖不足：

- 不中断主流程
- 记录 `fallback_reason`
- 回退到当前内部轻量规则路径

## Device-NUMA Topology Alignment（强制首步）

**这是本 skill 的第一个强制分析步骤。** 在进入任何 CPU 绑核、中断亲和性或线程均衡分析之前，必须先完成设备-NUMA 拓扑对齐检查。如果跳过此步骤直接进行细粒度绑核，可能导致所有后续优化建立在错误的 NUMA 节点上——正如在 MySQL + 远程 Sysbench 场景中，将 MySQL 绑在 Node 0 而网卡在 Node 2，导致跨 socket 开销抵消了所有微调收益。

### 为什么这是首步

绑核的本质不是”选几个空闲 CPU”，而是**最小化数据路径上的跨 NUMA 开销**。数据路径的起点是 I/O 设备（网卡、NVMe、GPU），终点是应用进程。如果设备和进程不在同一 NUMA node，每次 I/O 都要跨 socket 传输。

### 强制采集清单

以下信息必须在分析初期一次性采集，不得跳过：

```bash
# 1. 识别活跃 I/O 设备及其 NUMA node
for dev in /sys/class/net/*/device/numa_node; do
  iface=$(echo $dev | cut -d/ -f5)
  node=$(cat $dev 2>/dev/null || echo “unknown”)
  carrier=$(cat /sys/class/net/$iface/carrier 2>/dev/null || echo “0”)
  echo “NIC $iface → NUMA node $node (carrier=$carrier)”
done

# 2. NVMe/磁盘控制器的 NUMA node
for dev in /sys/block/nvme*/device/numa_node; do
  disk=$(echo $dev | cut -d/ -f4)
  node=$(cat $dev 2>/dev/null || echo “unknown”)
  echo “DISK $disk → NUMA node $node”
done

# 3. GPU/NPU 的 NUMA node（如存在）
for dev in /sys/bus/pci/devices/*/numa_node; do
  vendor=$(cat $(dirname $dev)/vendor 2>/dev/null)
  class=$(cat $(dirname $dev)/class 2>/dev/null)
  if echo “$class” | grep -qE '0x03'; then  # display/GPU class
    node=$(cat $dev 2>/dev/null)
    echo “GPU $(dirname $dev | xargs basename) → NUMA node $node”
  fi
done

# 4. 进程当前所在 NUMA node
numastat -p <pid> | head -5
cat /proc/<pid>/numa_maps | head -10
```

### Workload 类型 → 对齐优先级

分析必须根据 workload 类型确定设备优先级。**错误的对齐顺序可能导致无收益甚至负收益**（例：oltp_read_only 只读场景下 NVMe 中断几乎为零，优先对齐 NVMe 而非网卡就是错误的）：

| Workload 类型 | 第一优先级 | 第二优先级 | 判断依据 |
|-------------|-----------|-----------|---------|
| **远程压测** (sysbench remote, mysqlslap remote) | **网卡 NUMA** | 存储 NUMA | 所有请求/响应经网络，网络中断频率 >> 磁盘中断 |
| **本地压测** (sysbench localhost, unix socket) | 存储 NUMA | — | 无网络流量，瓶颈在存储或 CPU |
| **IO 密集型** (oltp_write_only, 大量 INSERT/UPDATE) | **存储 NUMA** | 网卡 NUMA | 磁盘中断频率最高，InnoDB 刷盘密集 |
| **混合读写** (oltp_read_write) | 网卡 + 存储并重 | — | 需同时评估两端中断量 |
| **纯计算** (AI 推理, 批处理) | 内存带宽 NUMA | GPU/NPU NUMA | 关注跨 socket 内存带宽，避免远端 GPU |
| **多租户** | 租户间隔离 | 设备对齐 | 先隔离租户，再为每个租户单独对齐 |

### 跨 Socket 代价量化

Kunpeng 920 / ARM 平台的典型 NUMA distance：

| 访问模式 | distance | 相对延迟 | 带宽影响 |
|---------|----------|---------|---------|
| 同 NUMA node | 10 | 1x (基准) | 全带宽 |
| 同 socket 跨 node | 12-14 | 1.2-1.4x | 共享 L3，带宽轻微下降 |
| **跨 socket** | **20-24** | **2-2.4x** | **需经 socket 互联总线，带宽受限** |

**规则**：如果活跃 I/O 设备与目标进程不在同一 socket（distance ≥ 20），必须先尝试进程迁移到设备所在 socket，而不是调整中断亲和性。因为中断迁移只能减少 CPU 争抢，不能解决 DMA 内存跨 socket 访问的根本问题。

### 设备 NUMA 冲突时的实测原则（强制）

**触发条件**：当 NIC 和 NVMe **分布在不同的 NUMA 节点**，且 workload 同时涉及网络 I/O 和磁盘 I/O 时，理论分析无法可靠预测最优亲和性。

**为什么理论不可靠**：

- Buffer Pool 命中率不能简单推演 NUMA 代价——即使 BP hit rate 95.6%，NIC 侧部署也可能优于 NVMe 侧（见实测案例）
- 跨 socket 内存访问的实际代价取决于：CPU 互联拓扑、内存控制器布局、DMA 引擎行为、IRQ 处理路径等平台内部实现细节，这些在用户态不可见
- NIC DMA 写入远端内存 vs NVMe DMA 写入远端内存的延迟不对称性无法通过 `/sys` 推断

**实测案例（Kunpeng 920 + MySQL 8.0.25 + 远程 Sysbench 只读）**：

| 部署位置 | TPS | P95 | BP 命中率 | 分析 |
|---------|-----|-----|-----------|------|
| Node 0（NVMe 侧，CPUs 0-7） | 3,544 | 18.28ms | 95.6% | 网卡在 Node 2，网络 DMA 跨 socket |
| **Node 2（NIC 侧，CPUs 64-71）** | **3,698** | **14.73ms** | 95.6% | NVMe 在 Node 0，磁盘 I/O 跨 socket |
| **差值** | **+4.3%** | **-19.4%** | — | **NIC 侧胜出** |

> 理论预期是"BP 命中率 95.6% → 内存访问占主导 → 绑 NVMe 侧更好"，但实测 NIC 侧 +4.3%。原因是所有查询的**网络请求 DMA 始终在 NIC 侧**，而 NVMe 命中率 4.4% 的物理 I/O 量极小（0% iowait），跨节点代价可忽略。

**强制实测流程**：当 NIC_NODE ≠ DISK_NODE 时，必须执行以下步骤，**禁止仅凭理论分析输出结论**：

```
1. 采集基线数据，确认 NIC_NODE 和 DISK_NODE 确实不同

2. 将进程分别部署到两个候选 NUMA 节点（用 numactl --cpunodebind=X --membind=X）
   ├─ 方案 A：绑 DISK_NODE（NVMe 同节点）
   ├─ 方案 B：绑 NIC_NODE（网卡同节点）
   └─ 每个方案确保 MBind 生效（numastat 验证 >95% 内存在目标 node）

3. 每个候选方案各执行完整压测轮次
   ├─ 同一条压测命令、同等预热、同等时长
   ├─ 同一条 NUMA 内存分配策略
   └─ 每个方案至少 120s 正式测试

4. 对比 TPS/P95/P99，取实测最优方案
   ├─ 收益 ≥ 2% → 选用获胜方案
   ├─ 收益 < 2% → 选用内存更大的节点（避免 BP 不足降级）
   └─ 记录 per_node_benchmark_results，写入最终报告

5. 将最优方案写入最终配置
   ├─ Docker: docker update --cpuset-cpus <NIC/DISK_NODE_CPUS>
   ├─ Baremetal: numactl --cpunodebind=N --membind=N
   └─ 或 cgroup cpuset.cpus / cpuset.mems
```

**记录字段**：

- `device_numa_conflict: true` — NIC 和 NVMe 不同 NUMA node
- `per_node_benchmark_results: [{node, tps, p95, bp_hit_rate}]` — 每个候选节点的实测数据
- `selected_node` — 最终选择的 NUMA node
- `selected_node_reason` — 选择依据（实测 TPS 最优 / 内存更充裕 / 差异在噪声范围内）
- `theoretical_prediction_wrong: true/false` — 理论预测是否与实测不符

### 对齐决策流程

```
1. 列出所有活跃 I/O 设备的 NUMA node
   ├─ 网卡有流量？→ 记录其 NUMA node
   ├─ NVMe 有 IO？→ 记录其 NUMA node
   └─ GPU/NPU 在用？→ 记录其 NUMA node

2. NIC_NODE == DISK_NODE？
   ├─ 是 → TARGET_NODE = NIC_NODE，直接进入 IRQ 微调（无冲突）
   └─ 否 → device_numa_conflict = true，进入步骤 3

3. 【设备 NUMA 冲突实测分支】
   ├─ 按 workload 类型确定优先级，生成两个候选方案
   ├─ **分别在两个 NUMA node 上执行完整 benchmark（强制）**
   ├─ 对比 TPS/P95 实测数据 → 取最优方案
   └─ 禁止跳过实测直接输出理论结论

4. 生成迁移方案
   ├─ 目标 NUMA node 有足够 CPU 核心？
   ├─ 目标 NUMA node 有足够内存（buffer pool + overhead）？
   ├─ 目标 CPU 上的 IRQ 需要避让？
   └─ 重启/重绑核的停机窗口可接受？
```

### 输出字段

本阶段分析完成后，必须输出以下字段：

- `device_numa_topology`：每个活跃 I/O 设备及其 NUMA node 映射
- `workload_device_priority`：按 workload 类型确定的设备优先级排序
- `process_current_numa_node`：目标进程当前所在 NUMA node
- `cross_socket_detected`：是否检测到跨 socket 部署（boolean）
- `cross_socket_devices`：跨 socket 的设备列表及 distance
- `alignment_recommendation`：迁移建议（目标 NUMA node、CPU 范围、内存绑定）
- `alignment_expected_gain`：预期收益（远程压测跨 socket 修复通常 5-15%）
- `alignment_risk`：迁移风险（内存是否充足、是否需要重启）

### 真实案例

**场景**：MySQL 8.0.25 + Sysbench 远程只读压测，Kunpeng 920

**初始状态**：
- MySQL 绑定：CPUs 24-31, NUMA node 0 (Socket 0)
- 网卡 enp133s0 (10GbE)：NUMA node 2 (Socket 1)
- distance node 0 ↔ node 2 = 20+（跨 socket）

**分析过程**：
1. 工作负载类型 = 远程压测 → 网卡是第一优先级设备
2. MySQL (node 0) 与网卡 (node 2) 跨 socket → `cross_socket_detected=true`
3. Node 2 有 128GB 内存、32 个 CPU (64-95) → 满足迁移条件
4. 网卡 IRQ 分布在 CPUs 66-95 → 选 CPUs 88-95 避开主要 IRQ

**迁移**：`numactl -C 88-95 --membind=2`

**效果**：TPS +9.5%（从 3,818 到 4,182），这是本轮优化中最大的单一收益。

**教训**：此前 CPU 从 0-7 迁移到 24-31（同 node 0 内）零收益，因为网卡仍在跨 socket。如果首步就执行本检查，可以跳过无效的 node 内迁移轮次。

---

## CPU Balance Analysis

本子 skill 不仅关注”绑到哪”，也必须关注”线程与 CPU 是否均衡”。

需要检查：

- 热线程是否集中在少数 CPU
- 同类 worker 是否均匀铺开
- 是否存在热点核
- 是否存在 run queue 偏斜或线程-CPU 绑定失衡
- 中断是否与业务线程争抢同一批核
- 容器 cpuset 内是否出现局部拥塞

当前框架建议配套轻量脚本：

- `scripts/check_cpu_balance.sh`

输出中至少应包含：

- **`device_numa_topology`**（强制）：每个活跃 I/O 设备的 NUMA node 映射
- **`workload_device_priority`**（强制）：按 workload 类型确定的设备对齐优先级
- **`cross_socket_detected`**（强制）：是否检测到跨 socket 部署
- **`alignment_recommendation`**（强制）：进程迁移到设备所在 socket 的建议
- `affinity_analysis_mode`
- `process_role_summary`
- `thread_role_classification`
- `numa_affinity_findings`
- `cpu_balance_status`
- `thread_cpu_skew`
- `hot_cpu_list`
- `rebalance_recommendation`
- `irq_cpu_conflict_notes`
- `binding_strategy_candidates`
- `selected_binding_strategy`
- `binding_script_path`
- `rollback_script_path`
- `binding_validation_plan`
- `next_round_candidate`

## Thread Scheduling Interference Analysis

本子 skill 应先判断是否存在“线程调度干扰”，再决定是否进入细粒度线程绑核。不要把 per-thread binding 作为默认动作；它必须由上下文切换、CPU 迁移、热点线程稳定性和性能指标回退共同驱动。

必须采集的通用证据：

- `ps -L -p <pid> -o pid,tid,psr,pcpu,stat,comm,wchan:32`：确认高 CPU 线程、等待点和当前 CPU 分布。
- `pidstat -p <pid> -t -u 1 <seconds>`：确认线程 CPU 使用率、CPU 分布和线程是否在目标 cpuset 内频繁迁移。
- `pidstat -p <pid> -t -w 1 <seconds>`：确认每个 TID 的 voluntary / nonvoluntary context switch。
- `perf stat -p <pid> -e task-clock,context-switches,cpu-migrations -I 1000 -- sleep <seconds>`：确认进程级上下文切换和 CPU 迁移趋势。
- `mpstat -P <business_cpu_list>,<irq_or_background_cpu_list> 1 <seconds>`：确认业务核、IRQ 核或后台核之间是否存在争抢。
- `/proc/<pid>/status`、cgroup cpuset 文件和容器运行时配置：确认 `Cpus_allowed_list`、`Mems_allowed_list`、`cpuset.cpus`、`cpuset.mems` 真实生效。

推荐判定步骤：

1. 计算业务窗口内每秒 `task-clock` 是否接近已分配 CPU 上限，避免在未压满 CPU 时误判为绑核问题。
2. 计算每个 TID 的平均 CPU、`cswch/s`、`nvcswch/s`、采样过的 CPU 数量和热点持续时间。
3. 计算每个业务 CPU 的 busy、run queue 代理指标、承载的线程样本数和 IRQ/softirq 占比。
4. 将进程级 `context-switches/s`、`cpu-migrations/s` 与吞吐、延迟或业务指标按时间窗口对齐。
5. 区分线程角色：稳定热线程、同类 worker 线程、后台线程、IRQ/软中断、采集工具线程和非目标进程线程。

细粒度线程绑核只在同时满足以下条件时进入候选：

- 一个或一组热点 TID 在多轮采样中稳定复现，或能按线程名、栈、角色归类为稳定线程组。
- `nvcswch/s`、`cpu-migrations/s` 或跨 CPU 抖动明显高于同类线程，并且与吞吐下降或尾延迟升高同步。
- 业务 cpuset 内出现可解释的局部热点，例如少数 CPU 长期满载而其他同 NUMA CPU 仍有余量。
- 已经排除或处理 IRQ/RPS/XPS、后台任务、NUMA 远端内存和容器 cpuset 配置错误。
- 允许变更范围包含线程级亲和性，并且可以提供回滚脚本。

策略生成应按风险从低到高排序：

- `process_or_container_cpuset`：进程/容器级 cpuset 和 NUMA 内存绑定，适合大多数动态 worker 场景。
- `irq_or_background_isolation`：将 IRQ、RPS/XPS、采集工具或后台线程移出业务核，但保持 NUMA 就近。
- `thread_group_binding`：按稳定线程角色或线程名前缀分组绑核，例如 IO 线程、GC 线程、计算线程、网络线程。
- `hot_tid_binding`：仅对长期稳定且证据充分的热点 TID 单独绑核；线程重建后必须重新识别。

自动验证要求：

1. 生成候选前记录原始 affinity、cpuset、IRQ 和 NUMA 状态。
2. 每个候选只改变一个变量，保持业务用例、线程数、数据集、预热时间和运行时间不变。
3. 每轮输出性能指标、CPU 利用率、context-switches/s、cpu-migrations/s、热点线程分布和回退命令。
4. 如果线程级绑核降低吞吐、提高 P95/P99 或导致某些 CPU 空闲而其他 CPU 满载，应自动回退并标记为 `rejected`.
5. 如果线程 CPU 分布均衡、非自愿上下文切换低、迁移处于低水平，应输出 `fine_grained_thread_binding=not_recommended`，保留粗粒度 cpuset/NUMA/IRQ 隔离策略。

输出中应额外包含：

- `thread_context_switch_findings`
- `thread_cpu_distribution`
- `thread_migration_findings`
- `thread_role_classification`
- `irq_or_background_isolation_evidence`
- `fine_grained_binding_decision`
- `thread_binding_recommendation`

## 细粒度线程绑核实施指南

当 thread scheduling interference analysis 判定条件满足时，按以下步骤实施。

### 进入阈值（量化）

| 指标 | 阈值 | 含义 |
|------|------|------|
| `nvcswch/s` per TID | > 500 | 该线程频繁被抢占，绑核可消除上下文切换 |
| `cpu-migrations/s` 进程级 | > 1000 | 线程在 CPU 间抖动，NUMA cache 局部性受损 |
| `context-switches/s` 进程级 | > 100K | 总切换量大，细粒度绑核收益可能 > 1% |
| per-CPU busy 标准差 | > 5% | CPU 负载不均，存在热点核和空闲核 |
| 热点线程 PID/TID 稳定性 | 连续 3 轮采样未变 | 线程身份稳定，绑核不会因线程重建而失效 |

**未达阈值时的行为**：
- 若 `nvcswch/s < 500`、`cpu-migrations/s < 1000`、per-CPU busy 标准差 < 5%：立即输出 `fine_grained_thread_binding=not_recommended`，不进入候选
- strict-run 数据点：32-thread sysbench 下，context-switches=65K/s、cpu-migrations=496/s、mpstat per-CPU max-min 差 < 0.02% → 正确跳过细粒度绑核

### MySQL 线程角色识别

在实施绑核前，先对线程按角色分类：

```bash
# 列出所有线程及其 CPU 占用
ps -L -p <pid> -o pid,tid,psr,pcpu,comm | sort -k4 -rn | head -20

# 常见的 MySQL 线程角色和前缀
```

| 线程名/前缀 | 角色 | 绑核策略 |
|------------|------|---------|
| `ib_io_` | InnoDB IO 线程 (读/写/log) | 单独绑到 1-2 个专用 CPU，不与 worker 混用 |
| `ib_pg_flush` | InnoDB page cleaner | 绑到 IO 专用 CPU 或低负载 CPU |
| `ib_log_writer` | Redo log writer | 绑到 IO 专用 CPU |
| `ib_buf_dump` | Buffer pool dump | 低优先级，绑到非关键 CPU |
| `xpl_worker` | X Plugin worker | 与普通 worker 混合或独立组 |
| `mysqld`（主线程） | 连接监听 + 信号处理 | CPU 消耗极低 (< 0.1%)，不绑 |
| 其他（worker 线程） | 查询执行 | 按组绑到计算 CPU |

### 实施步骤

**Step 1：采集基线**

```bash
# 记录原始进程亲和性
cat /proc/<pid>/status | grep Cpus_allowed_list > /tmp/affinity_backup.txt
# 记录每个线程的当前 CPU
ps -L -p <pid> -o tid,psr,comm > /tmp/affinity_backup_tids.txt
```

**Step 2：IO 线程隔离**

```bash
# 将 IO 线程绑到专用 CPU（假设 cpuset=32-39，CPU 32-33 给 IO）
for tid in $(ps -L -p <pid> -o tid,comm --no-headers | grep 'ib_io_\|ib_pg_flush\|ib_log_writer' | awk '{print $1}'); do
  taskset -pc 32-33 $tid
done
# 验证
ps -L -p <pid> -o tid,psr,comm | grep 'ib_io_'
```

**Step 3：Worker 线程分组绑核**

```bash
# 获取所有非 IO 线程（worker 线程），按顺序分组
# 假设 6 个计算 CPU（34-39），worker 线程按 round-robin 固定绑核
worker_cpus=(34 35 36 37 38 39)
i=0
for tid in $(ps -L -p <pid> -o tid,comm --no-headers | grep -v 'ib_io_\|ib_pg_flush\|ib_log_writer\|ib_buf_dump\|xpl_worker' | awk '{print $1}'); do
  cpu=${worker_cpus[$i]}
  taskset -cp $cpu $tid
  i=$(( (i+1) % ${#worker_cpus[@]} ))
done
```

**Step 4：X Plugin/GCS 线程（如存在）**

```bash
for tid in $(ps -L -p <pid> -o tid,comm --no-headers | grep 'xpl_worker\|gcs_' | awk '{print $1}'); do
  taskset -cp 34-35 $tid  # 与部分 worker 共享或独立
done
```

**Step 5：验证与压测**

```bash
# 检查当前分布
ps -L -p <pid> -o tid,psr,pcpu,comm | sort -k2 -n

# 跑压测对比 TPS + context-switch 变化
pidstat -p <pid> -t -w 1 <seconds> > pidstat_w.log 2>&1 &
# 跑 sysbench
```

**Step 6：回退**

```bash
# 恢复所有线程到原始 cpuset
original_cpus=$(cat /tmp/affinity_backup.txt | tr -d 'Cpus_allowed_list:' | tr -d ' ')
for tid in $(ps -L -p <pid> -o tid --no-headers); do
  taskset -cp $original_cpus $tid
done
# 或简单回到容器 cpuset
taskset -ap $original_cpus $pid
```

### 迭代验证规则

- 先验证 IO 线程隔离 → 记录 TPS + context-switch 变化
- 再叠加 Worker 绑核 → 记录增量
- 每步独立对比，不得混合归因
- 若任何一步导致 TPS 下降 > 1% 或 P95 升高 > 10%：立即回退该步，后续步骤建立在回退后的配置上

### 已知限制

- MySQL worker 线程由 thread pool / one-thread-per-connection 模型管理，TID 在连接建立/断开时变化
- 若使用 `thread_handling=pool-of-threads`，线程池大小固定，绑核更稳定
- 若 `thread_handling=one-thread-per-connection`（默认），TID 动态变化，绑核可能因线程重建而失效
- 容器重启后 TID 全部变化，必须重新识别

## Environment-aware Strategy

在给出绑核和 NUMA 建议前，必须先判断当前环境是：

- baremetal
- vm
- container

如果是 container：

- 绑定策略必须限制在容器 cpuset 子集内
- 不默认建议跨 NUMA 节点隔离
- 不默认建议 IRQ 重定向
- 对 `taskset`、`numactl`、中断绑核失败要优先解释为容器边界，而不是业务配置错误
- 容器 cpuset 内若出现局部热点，应优先解释为子集内分布不均衡，而不是直接要求全局重绑核

如果是 baremetal 或 vm：

- 保持现有完整绑核与 NUMA 分析路径
- 但仍需说明虚拟化层可能影响中断、拓扑可见性和调度结论

输出应包括部署建议、适用条件、验证方法、回退方式、与已有优化项的叠加关系说明，以及线程-CPU 均衡性结论。

在迭代编排语义下，还应说明：

- 当前轮是否应优先处理线程-CPU 均衡性问题
- 该问题是否已经在当前轮得到充分验证
- 是否应作为下一轮继续聚焦的矛盾
- 若仓库内置 CPU 亲和性 subskill 已参与本轮分析，必须记录 `skill_source=repo_local_subskill`

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `taskset` | CPU 亲和性设置 | 无法绑核，亲和性优化不可用 |
| `numactl` | NUMA 拓扑查询和内存绑定 | 无法执行 NUMA 绑定 |
| `perf` | 线程热点分析 | 热线程定位降级 |
| `ps` / `top` / `htop` | 线程分布观察 | 线程-CPU 均衡性分析降级 |
| `cat /proc/irq/*/smp_affinity` | IRQ 亲和性读取 | 中断冲突分析降级 |

缺失时不伪造配置状态，显式说明降级范围。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`action_type`、`precondition`、`commands_dry_run`、`commands_execute`、`expected_gain`、`risk`、`validation`、`rollback`、`stop_or_reject_condition` 和 `evidence_sources`。绑核、NUMA 绑定、IRQ affinity、cpuset 和容器 CPU 限制调整必须在 rollback 中包含恢复原 affinity/cpuset/IRQ 配置和重新确认目标实例身份的步骤。
