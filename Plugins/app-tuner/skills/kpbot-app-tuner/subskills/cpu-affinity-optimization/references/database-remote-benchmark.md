# MySQL + 远程 Sysbench 设备-NUMA 对齐实测案例

本文件记录 MySQL + 远程压测场景的设备-NUMA 对齐实测经验，作为 `SKILL.md` 中 Device-NUMA Topology Alignment 的实证支撑。

## 案例 1：NIC vs NVMe 不同 NUMA node 的实测选择

**场景**：MySQL 8.0.25 + Sysbench 远程只读压测，Kunpeng 920

**初始状态**：
- MySQL 绑定：CPUs 24-31, NUMA node 0 (Socket 0)
- 网卡 enp133s0 (10GbE)：NUMA node 2 (Socket 1)
- NVMe：NUMA node 0 (Socket 0)
- distance node 0 ↔ node 2 = 20+（跨 socket）

**实测数据**：

| 部署位置 | TPS | P95 | BP 命中率 | 分析 |
|---|---|---|---|---|
| Node 0（NVMe 侧，CPUs 0-7） | 3,544 | 18.28ms | 95.6% | 网卡在 Node 2，网络 DMA 跨 socket |
| **Node 2（NIC 侧，CPUs 64-71）** | **3,698** | **14.73ms** | 95.6% | NVMe 在 Node 0，磁盘 I/O 跨 socket |
| **差值** | **+4.3%** | **-19.4%** | — | **NIC 侧胜出** |

**理论预期与实测不符**：
- 理论：BP 命中率 95.6% → 内存访问占主导 → 绑 NVMe 侧更好
- 实测：NIC 侧 +4.3%
- 原因：所有查询的**网络请求 DMA 始终在 NIC 侧**，而 NVMe 命中率 4.4% 的物理 I/O 量极小（0% iowait），跨节点代价可忽略

**强制实测流程**：当 NIC_NODE ≠ DISK_NODE 时，必须分别在两个候选 NUMA node 上执行完整 benchmark，**禁止仅凭理论分析输出结论**。

---

## 案例 2：进程迁移到网卡所在 socket

**场景**：MySQL 8.0.25 + Sysbench 远程只读，Kunpeng 920

**初始状态**：
- MySQL 绑定：CPUs 24-31, NUMA node 0
- 网卡 enp133s0：NUMA node 2（跨 socket）

**分析**：
1. workload 类型 = 远程压测 → 网卡是第一优先级
2. MySQL (node 0) 与网卡 (node 2) 跨 socket → `cross_socket_detected=true`
3. Node 2 有 128GB 内存、32 个 CPU (64-95) → 满足迁移条件
4. 网卡 IRQ 分布在 CPUs 66-95 → 选 CPUs 88-95 避开主要 IRQ

**迁移**：`numactl -C 88-95 --membind=2`

**效果**：TPS +9.5%（从 3,818 到 4,182），本轮优化中最大单一收益。

**教训**：
- 此前 CPU 从 0-7 迁移到 24-31（同 node 0 内）零收益，因为网卡仍在跨 socket
- 如果首步就执行设备-NUMA 对齐检查，可以跳过无效的 node 内迁移轮次

---

## 实测流程规范

当 NIC_NODE ≠ DISK_NODE 时，必须执行：

```
1. 采集基线数据，确认 NIC_NODE 和 DISK_NODE 确实不同

2. 将进程分别部署到两个候选 NUMA 节点
   ├─ 方案 A：绑 DISK_NODE（NVMe 同节点）
   ├─ 方案 B：绑 NIC_NODE（网卡同节点）
   └─ 每个方案确保 MBind 生效（numastat 验证 >95% 内存在目标 node）

3. 每个候选方案各执行完整压测轮次
   ├─ 同一条压测命令、同等预热、同等时长
   ├─ 每个方案至少 120s 正式测试

4. 对比 TPS/P95/P99，取实测最优方案
   ├─ 收益 ≥ 2% → 选用获胜方案
   ├─ 收益 < 2% → 选用内存更大的节点
   └─ 记录 per_node_benchmark_results

5. 将最优方案写入最终配置
   ├─ Docker: docker update --cpuset-cpus <NIC/DISK_NODE_CPUS>
   ├─ Baremetal: numactl --cpunodebind=N --membind=N
   └─ 或 cgroup cpuset.cpus / cpuset.mems
```

**记录字段**：
- `device_numa_conflict: true`
- `per_node_benchmark_results: [{node, tps, p95, bp_hit_rate}]`
- `selected_node` — 最终选择的 NUMA node
- `selected_node_reason` — 选择依据
- `theoretical_prediction_wrong: true/false`
