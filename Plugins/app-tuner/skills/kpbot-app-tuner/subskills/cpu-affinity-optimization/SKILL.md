---
name: cpu-affinity-optimization
description: 在确认瓶颈主要位于 CPU 侧后，基于线程、NUMA 和中断分布进行绑核、绑内存与中断亲和性优化，作为 kpbot-app-tuner 的子 skill 使用。
---

# CPU Affinity Optimization

当主瓶颈已经落到 CPU 侧后，优先使用本子 skill。

本子 skill 是统一入口：

- 通用 CPU 亲和性分析入口
- skill 内置 `ref-skills/cpu-affinity-optimization` 的适配层
- 平台判断、依赖检查和回退决策中心

实战经验与案例存放在 `references/` 目录：

- `references/ascend-vllm-binding.md` — Ascend NPU + vLLM 线程绑核实战案例、线程过提交分析、多卡 TP 绑核
- `references/database-remote-benchmark.md` — MySQL + 远程 Sysbench 设备-NUMA 对齐实测案例

## Recommended Inputs

- `target_pid` — 目标进程 PID
- `thread_cpu_snapshot` — 线程与 CPU 分布快照
- `architecture` — CPU 架构
- 当前 NUMA 拓扑（核数、节点数、NUMA distances）
- **活跃 I/O 设备的 NUMA node**（网卡、NVMe、GPU/NPU）
- **workload 类型**（`remote_benchmark` / `local_benchmark` / `io_intensive` / `compute` / `mixed` / `multi_tenant` / `npu_llm_infer`）
- 当前环境类型（`baremetal` / `vm` / `container`）
- 容器 cpuset 范围（容器环境时）
- `change_scope` — 用户允许的变更范围
- `restart_allowed` — 是否允许重启

## Repo-local Integration

默认优先接入 `ref-skills/cpu-affinity-optimization`（入口文件 `SKILL.md`，脚本目录 `scripts/`）。如果缺失或依赖不足，记录 `fallback_reason` 并回退到当前内部轻量规则路径。

## Device-NUMA Topology Alignment（强制首步）

绑核的本质是**最小化数据路径上的跨 NUMA 开销**。如果设备和进程不在同一 NUMA node，每次 I/O 都要跨 socket 传输。

### 强制采集清单

```bash
# 1. 网卡 NUMA node
for dev in /sys/class/net/*/device/numa_node; do
  iface=$(echo $dev | cut -d/ -f5)
  node=$(cat $dev 2>/dev/null || echo "unknown")
  carrier=$(cat /sys/class/net/$iface/carrier 2>/dev/null || echo "0")
  echo "NIC $iface -> NUMA node $node (carrier=$carrier)"
done

# 2. NVMe/磁盘 NUMA node
for dev in /sys/block/nvme*/device/numa_node; do
  disk=$(echo $dev | cut -d/ -f4)
  node=$(cat $dev 2>/dev/null || echo "unknown")
  echo "DISK $disk -> NUMA node $node"
done

# 3. NPU/GPU NUMA node（见下方 NPU 专项采集）

# 4. 进程当前所在 NUMA node
numastat -p <pid> | head -5
cat /proc/<pid>/numa_maps | head -10
```

### Workload 类型 → 对齐优先级

| Workload 类型 | 第一优先级 | 第二优先级 |
|---|---|---|
| **远程压测** | **网卡 NUMA** | 存储 NUMA |
| **本地压测** | 存储 NUMA | — |
| **IO 密集型** | **存储 NUMA** | 网卡 NUMA |
| **混合读写** | 网卡 + 存储并重 | — |
| **纯计算 / NPU 推理** | NPU NUMA | 内存带宽 NUMA |
| **多租户** | 租户间隔离 | 设备对齐 |

### 跨 Socket 代价

| 访问模式 | distance | 相对延迟 |
|---|---|---|
| 同 NUMA node | 10 | 1x |
| 同 socket 跨 node | 12-14 | 1.2-1.4x |
| **跨 socket** | **20-24** | **2-2.4x** |

**规则**：如果活跃 I/O 设备与目标进程不在同一 socket（distance ≥ 20），必须先尝试进程迁移到设备所在 socket。

### 设备 NUMA 冲突实测原则

当 NIC 和 NVMe 分布在不同 NUMA 节点，且 workload 同时涉及网络 I/O 和磁盘 I/O 时，**禁止仅凭理论分析输出结论**，必须分别在两个候选 NUMA node 上执行完整 benchmark，取实测最优方案。详见 `references/database-remote-benchmark.md`。

### 对齐决策流程

```
1. 列出所有活跃 I/O 设备的 NUMA node
2. NIC_NODE == DISK_NODE？
   ├─ 是 → TARGET_NODE = NIC_NODE，直接进入 IRQ 微调
   └─ 否 → device_numa_conflict = true，进入实测分支
3. 分别在两个候选 NUMA node 上执行完整 benchmark（强制）
4. 对比 TPS/P95 实测数据 → 取最优方案
5. 生成迁移方案（numactl --cpunodebind=N --membind=N 或 docker update --cpuset-cpus）
```

### 输出字段

- `device_numa_topology`：每个活跃 I/O 设备及其 NUMA node
- `workload_device_priority`：按 workload 类型确定的设备优先级
- `process_current_numa_node`：目标进程当前 NUMA node
- `cross_socket_detected`：是否跨 socket 部署
- `alignment_recommendation`：迁移建议
- `alignment_expected_gain`：预期收益

## NPU 设备 NUMA 拓扑采集

> 本节是 Device-NUMA Topology Alignment 的 NPU 专项补充。

### Ascend NPU NUMA node 查询

**重要**：`/sys/bus/pci/devices/*/numa_node` 对 Ascend NPU 可能全部返回 node 0（驱动不正确暴露 PCIe NUMA node），必须用以下 fallback 方法交叉验证：

```bash
# 方法 1：PCI vendor id 过滤（19e5 = Huawei Ascend）— 可能不可靠
lspci -d 19e5: -v | grep -E 'NUMA|Device|Slot'

# 方法 2：/sys 查询 — 可能全部返回 0，需 fallback
for dev in /sys/bus/pci/devices/*/numa_node; do
  vendor=$(cat $(dirname $dev)/vendor 2>/dev/null)
  if [ "$vendor" = "0x19e5" ]; then
    bdf=$(basename $(dirname $dev))
    echo "Ascend NPU $bdf -> NUMA node $(cat $dev)"
  fi
done

# 方法 3（推荐）：npu-smi 拓扑 + chip→NUMA 映射
npu-smi info -m   # 获取 NPU ID、Chip ID、Chip Phy-ID 映射
npu-smi info -l   # 获取拓扑
```

### Ascend chip → NUMA node 映射（经验表）

当 `/sys` 返回值不可靠时，用以下经验方法推断：

| 推断方法 | 说明 |
|---|---|
| `npu-smi info -m` 获取 Chip Phy-ID | Phy-ID 与物理 PCIe slot 对应 |
| `lspci -d 19e5:` 获取 BDF 和 slot | slot 位置对应 NUMA node |
| 实测绑核验证 | 在候选 NUMA node 上绑核跑 benchmark，收益最高的即为正确 node |

**实测验证流程**（当 `/sys` 不可靠时强制执行）：

```bash
# 1. 获取 chip 列表
npu-smi info -m

# 2. 对每个候选 NUMA node 尝试绑核
for node in 0 1 2 3; do
  # 获取该 node 的 CPU 范围
  node_cpus=$(numactl -H | grep "node $node cpus" | sed 's/.*cpus: //')
  # 绑核 + 跑 benchmark
  taskset -c $node_cpus <benchmark_cmd>
  echo "NUMA node $node: <throughput>"
done
# 取最优 node
```

### NVIDIA GPU NUMA node 查询

```bash
nvidia-smi topo -m
for dev in /sys/bus/pci/devices/*/numa_node; do
  vendor=$(cat $(dirname $dev)/vendor 2>/dev/null)
  if [ "$vendor" = "0x10de" ]; then
    bdf=$(basename $(dirname $dev))
    echo "NVIDIA GPU $bdf -> NUMA node $(cat $dev)"
  fi
done
```

### NPU 推理场景输出字段

- `npu_device_list`：每张 NPU 的 BDF、vendor、NUMA node
- `npu_numa_source`：NUMA node 来源（`/sys` / `npu-smi` / `实测验证`）
- `hbm_access_cross_socket`：host 进程是否跨 socket 访问 HBM
- `npu_binding_recommendation`：EngineCore / acl_thread 应绑的 CPU 范围

---

## NPU 推理进程线程角色识别

适用场景：vLLM / TGI / SGLang 等推理框架运行在 Ascend / NVIDIA NPU 上，且瓶颈已确认在 CPU 侧。

### vLLM AsyncEngine 多进程架构

**关键**：vLLM AsyncEngine 模式下，EngineCore 运行在**子进程**中，不是主进程的线程。线程识别必须先定位子进程 PID，再在子进程中识别线程角色。

```bash
# Step 1：找到主进程 PID
main_pid=$(pgrep -f 'vllm serve' | head -1)

# Step 2：找到 EngineCore 子进程（关键修复）
ec_pid=$(pgrep -P $main_pid | while read p; do
  comm=$(cat /proc/$p/comm 2>/dev/null)
  echo "$comm" | grep -q 'EngineCore' && echo "$p" && break
done)
# 或直接按进程名查找
ec_pid=$(pgrep -f 'VLLM::EngineCore' | head -1)

# Step 3：在子进程中识别线程角色（不是主进程！）
ps -L -p $ec_pid -o tid,psr,pcpu,comm --no-headers | sort -k3 -rn | head -30
```

### 线程角色分类

**vLLM 框架线程**（在 EngineCore 子进程中）：

| 线程名/前缀 | 角色 | CPU 特征 | 绑核策略 |
|---|---|---|---|
| `VLLM::EngineCor` | EngineCore 主线程 + worker | 高 CPU，推理主热点 | 绑在 NPU 所在 NUMA node |
| `acl_thread` | ACL 通信线程，host↔NPU 控制通路 | 中-高 CPU | 与 EngineCore 同 NUMA，**分核** |
| `release_thread` | 异步资源释放线程 | 低-中 CPU | 绑到同 NUMA 低优先级 CPU |

**Ascend NPU 驱动线程**（在 EngineCore 子进程中，版本相关，可能不全部出现）：

| 线程名/前缀 | 角色 | 备注 |
|---|---|---|
| `hccl` / `Hccl*` | HCCL 集合通信 | 仅多卡 TP 场景出现 |

> **注意**：`devmmgr`/`DevMemManage`、`gpu_callback`、`asyncio/uvloop` 等线程角色是否出现取决于 vLLM 和 CANN 版本，不一定在所有环境中观察到。识别时应以实际 `ps -L` 输出为准。

### 绑核策略表

按线程角色分配专用 CPU 池，**主线程与 worker 线程区分对待**：

| 线程角色 | CPU 池大小（每 NPU） | 绑核原则 | 与 NPU 的 NUMA 关系 |
|---|---|---|---|
| EngineCore 主线程（高 CPU） | 8 核 | 绑在 NPU 所在 NUMA node 低序号 CPU | 同 NUMA，最低 HBM 控制延迟 |
| acl_thread | 8 核 | 与 EngineCore 同 NUMA，**紧邻但分核** | 同 NUMA，host↔NPU 控制通路最短 |
| release_thread | 8 核 | 同 NUMA，低序号 CPU | 同 NUMA 但低优先级 |
| EngineCore worker 线程（低 CPU） | 不绑或分散 | 大量低 CPU worker 线程（800+）可统一绑到同 NUMA 的剩余 CPU，或仅绑进程级 cpuset | 同 NUMA |

**关键修复**：
- EngineCore 主线程（CPU 占用最高）单独绑 8 核
- acl_thread 单独绑相邻 8 核，**不可与主线程混绑**
- release_thread 单独绑 8 核
- 其余 800+ 低 CPU worker 线程：统一绑到同 NUMA node 的剩余 CPU 范围，或仅用进程级 `taskset -pc` 限制整个子进程

### 绑核实施命令

```bash
ec_pid=<EngineCore子进程PID>
npu_node_cpus="<NPU所在NUMA的CPU范围，如480-503>"

# 拆分 CPU 池
engine_pool="${npu_node_cpus%-*}-$(( ${npu_node_cpus%-*} + 7 ))"     # 如 480-487
acl_pool="$(( ${npu_node_cpus%-*} + 8 ))-$(( ${npu_node_cpus%-*} + 15 ))"  # 如 488-495
release_pool="$(( ${npu_node_cpus%-*} + 16 ))-$(( ${npu_node_cpus%-*} + 23 ))" # 如 496-503

# 识别三类关键线程（在子进程中！）
engine_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'VLLM::EngineCor' | awk '{print $1}')
acl_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'acl_thread' | awk '{print $1}')
release_tids=$(ps -L -p $ec_pid -o tid,comm --no-headers | grep 'release_thread' | awk '{print $1}')

# 绑核
for tid in $engine_tids; do taskset -pc $engine_pool $tid; done
for tid in $acl_tids; do taskset -pc $acl_pool $tid; done
for tid in $release_tids; do taskset -pc $release_pool $tid; done

# 验证
ps -L -p $ec_pid -o tid,psr,comm --no-headers | grep -E 'EngineCor|acl_thread|release' | sort -k2 -n
```

### 回退

```bash
# 恢复所有线程到容器 cpuset
original_cpus=$(cat /sys/fs/cgroup/cpuset.cpus 2>/dev/null || echo "0-639")
for tid in $(ps -L -p $ec_pid -o tid --no-headers); do
  taskset -pc $original_cpus $tid
done
```

---

## NPU 推理线程过提交分析

vLLM 默认创建 800+ 线程，但实际推理只用 80-100 个。冗余线程（OMP worker、ACL 线程池、asyncio 后台）导致调度开销上升、NUMA 局部性被破坏。

### 识别方法

```bash
# 统计总线程数 vs 实际活跃线程数
total=$(ps -L -p <pid> --no-headers | wc -l)
active=$(ps -L -p <pid> -o tid,pcpu,stat --no-headers | awk '$2 > 0.1 && $3 ~ /R|S/ {count++} END {print count}')
echo "Total: $total, Active: $active, Redundant: $((total - active))"

# 检查 OMP 线程环境变量
cat /proc/<pid>/environ | tr '\0' '\n' | grep -E 'OMP_NUM_THREADS|MKL_NUM_THREADS'
```

### 边界声明

本 skill 只负责**识别**线程过提交现象并完成 CPU 绑核/NUMA 亲和性优化。`OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 等**线程数参数调优由 `application-config-optimization` 负责**。本 skill 在识别到过提交后，应在候选动作的 `rejection_criteria` 或 `expected_effect` 中标注"线程数参数调优建议路由到 application-config-optimization"，不得自行输出线程数参数变更动作。

> 实战案例（绑核部分）见 `references/ascend-vllm-binding.md`；线程数参数调优实证见 `../application-config-optimization/references/ascend-vllm-config.md`

---

## CPU Balance Analysis

需要检查：

- 热线程是否集中在少数 CPU
- 同类 worker 是否均匀铺开
- 是否存在热点核
- 中断是否与业务线程争抢同一批核
- 容器 cpuset 内是否出现局部拥塞

输出字段：`cpu_balance_status`、`thread_cpu_skew`、`hot_cpu_list`、`rebalance_recommendation`、`irq_cpu_conflict_notes`

## Thread Scheduling Interference Analysis

细粒度线程绑核不是默认动作，必须由上下文切换、CPU 迁移、热点线程稳定性和性能指标回退共同驱动。

### 进入阈值

| 指标 | 阈值 | 含义 |
|---|---|---|
| `nvcswch/s` per TID | > 500 | 线程频繁被抢占 |
| `cpu-migrations/s` 进程级 | > 1000 | 线程在 CPU 间抖动 |
| `context-switches/s` 进程级 | > 100K | 总切换量大 |
| per-CPU busy 标准差 | > 5% | CPU 负载不均 |
| 热点线程 TID 稳定性 | 连续 3 轮采样未变 | 线程身份稳定 |

**未达阈值**：输出 `fine_grained_thread_binding=not_recommended`，保留粗粒度 cpuset/NUMA/IRQ 隔离策略。

### 策略生成（按风险从低到高）

1. `process_or_container_cpuset`：进程/容器级 cpuset + NUMA 内存绑定
2. `irq_or_background_isolation`：IRQ/RPS/XPS/后台线程移出业务核
3. `thread_group_binding`：按稳定线程角色分组绑核
4. `hot_tid_binding`：仅对稳定热点 TID 单独绑核

### 必须采集的证据

```bash
ps -L -p <pid> -o pid,tid,psr,pcpu,stat,comm,wchan:32
pidstat -p <pid> -t -u 1 <seconds>    # CPU 使用率 + 迁移
pidstat -p <pid> -t -w 1 <seconds>    # 上下文切换
perf stat -p <pid> -e task-clock,context-switches,cpu-migrations -I 1000 -- sleep <seconds>
mpstat -P <cpu_list> 1 <seconds>
```

> 容器内 pidstat/mpstat 可能缺失，此时用 `ps -L` + `perf stat` 替代。

## Environment-aware Strategy

如果是 container：

- 绑定策略必须限制在容器 cpuset 子集内
- 不默认建议跨 NUMA 节点隔离或 IRQ 重定向
- 对 `taskset`/`numactl` 失败优先解释为容器边界
- 容器 cpuset 内局部热点优先解释为子集内分布不均衡

如果是 baremetal 或 vm：

- 保持完整绑核与 NUMA 分析路径
- 说明虚拟化层可能影响中断、拓扑可见性

## Dependencies

| 工具 | 用途 | 缺失影响 |
|------|------|---------|
| `taskset` | CPU 亲和性设置 | 无法绑核 |
| `numactl` | NUMA 拓扑查询和内存绑定 | 无法 NUMA 绑定 |
| `perf` | 线程热点分析 | 热线程定位降级 |
| `ps` / `top` | 线程分布观察 | 线程均衡性分析降级 |
| `pidstat` | 上下文切换 + CPU 迁移 | 调度干扰分析降级（可用 `perf stat` 替代） |
| `mpstat` | per-CPU 利用率 | CPU 均衡分析降级（可用 `ps -L` 替代） |

缺失时不伪造配置状态，显式说明降级范围。

## Candidate Action Contract

每个 `candidate_actions[]` 必须包含 `action_id`、`title`、`category`、`priority`、`change_mode`、`requires_root`、`risk`、`implementation_plan`、`validation_plan`、`rollback`、`expected_effect`、`expected_gain_metric`、`rejection_criteria` 和 `evidence_refs`。绑核、NUMA 绑定、IRQ affinity、cpuset 调整必须在 rollback 中包含恢复原配置步骤。

## 输出字段汇总

- `device_numa_topology`（强制）
- `workload_device_priority`（强制）
- `cross_socket_detected`（强制）
- `alignment_recommendation`（强制）
- `affinity_analysis_mode`
- `thread_role_classification`
- `numa_affinity_findings`
- `cpu_balance_status`
- `thread_cpu_skew`
- `hot_cpu_list`
- `rebalance_recommendation`
- `binding_strategy_candidates`
- `selected_binding_strategy`
- `binding_script_path`
- `rollback_script_path`
- `fine_grained_binding_decision`
- `next_round_candidate`
