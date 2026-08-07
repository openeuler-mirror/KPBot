# 端到端示例 / End-to-End Examples

以下提供两个典型优化场景的走查示例，帮助理解 skill 的实际执行流程。

## 示例 1：MySQL 远程只读场景优化（8U32G）

### 场景输入

```
scenario_description: "对 MySQL 8.0 做只读性能优化，远程 sysbench 压测"
application_name: "mysql-8.0"
workload_type: "database"
database_engine: "mysql"
target_resource_profile: "8U32G"
deployment_guide_provided: true
benchmark_script_provided: true
change_scope: "os,app_config"
restart_allowed: true
```

### 执行流程走查

#### Step 1: 环境备份

```bash
scripts/backup_environment.sh /tmp/optimization/env_backup
```

产出：CPU 拓扑（8 核 aarch64、NUMA 1 节点）、内存 32G、磁盘 NVMe、网卡 10GbE、OS 为 openEuler、容器环境检测为 `baremetal`。

#### Step 2: 场景准备

用户已提供部署指导和 sysbench 测试脚本。按材料部署 MySQL 8.0。

#### Step 3: 目标规格约束

解析 `8U32G`：
- CPU 约束：`taskset -c 0-7` 绑定到前 8 核
- 内存约束：`innodb_buffer_pool_size = 24G`（预留 8G 给 OS 和其他进程）
- 验证约束生效：确认 mysqld 只运行在 CPU 0-7 上

#### Step 4: 基线测试

```bash
scripts/init_report.sh /tmp/optimization/report "mysql-8u32g-readonly"
scripts/run_placeholder_benchmark.sh "mysql-8u32g-readonly"
```

产出：TPS = 3200，QPS = 64000，p99 latency = 12ms。

#### Step 5: 基线确认

向用户反馈：
- 基线 TPS: 3200
- 服务端 CPU 利用率: 72%（mpstat 每核 idle ~28%）
- 服务端内存: 85%
- 服务端磁盘: 15%
- 服务端网卡: 45%

用户确认 → 继续。

#### Step 6: 瓶颈识别

调用 `io-memory-network-bottleneck-analysis`：
- 初始结论为 `unknown_bottleneck` 或 CPU 证据不足，需要排查客户端与测试方法

但由于服务端 CPU 利用率仅 72% < 85%，进入客户端瓶颈排查：

- 客户端 CPU：单核 98%（sysbench 单进程瓶颈）
- 客户端网卡：35%
- 解决：切换为多进程 sysbench（4 进程 × 16 线程）

切换后服务端 CPU 上升到 88%。仍需网络调优推至饱和：

- 关闭防火墙 → CPU 上升到 91%
- RPS 重定向避开应用核 → CPU 上升到 95%
- mpstat 每核 idle <= 1% → 饱和确认通过

#### Step 7: 候选优化 skill 列表生成

最终瓶颈重新分类为 `cpu_bottleneck`。根据 `performance_signal_summary.json` 生成候选顺序：应用配置、性能库、CPU 亲和性；候选完成后追加 coverage skill：网络、编译、OS、BIOS、硬件升级、Other。

**Round 1**：应用配置优化

- `application-config-optimization` 触发数据库专项
- AHI 分析：`mysqld_cpu_pct=92, threads_per_core=8, buffer_pool_hit_rate=99.5%`
- 建议：关闭 AHI（`innodb_adaptive_hash_index=OFF`）
- 验证：TPS 3200 → 3650（+14.1%）
- `iteration_decision: continue`

**Round 2**：性能库选型

- 检测到 jemalloc 可用
- 建议：`LD_PRELOAD=/usr/lib64/libjemalloc.so.2`
- 验证：TPS 3650 → 3731（+2.2%）
- `iteration_decision: continue`

**Round 3**：OS 优化

- 建议：`sysctl -w vm.swappiness=1`、关闭 THP
- 验证：TPS 3731 → 3740（+0.2%，< 1%）
- `iteration_decision: continue`

**Round 4**：编译器优化

- 当前 GCC 版本 10.3，建议开启 `-O3 -mcpu=tsv110`
- 但用户表示无法重编译 → 跳过
- `iteration_decision: stop`

#### Step 9: 报告输出

```
综合收益：TPS 3200 → 3740（+16.9%）
主要贡献：AHI 关闭（+14.1%）、jemalloc（+2.2%）、OS 调优（+0.2%）
停止原因：OS skill 本轮收益 < 1%，但尚未达到单 skill 5 轮停止阈值；全局停止来自剩余 high/medium 动作无法执行（编译优化被用户拒绝）且用户要求输出报告
风险：AHI 关闭需验证业务查询模式无退化
回退：`SET GLOBAL innodb_adaptive_hash_index=ON`
```

---

## 示例 2：计算型工作负载优化（未提供部署材料）

### 场景输入

```
scenario_description: "优化一个图像处理服务的 CPU 吞吐"
application_name: "image-processor"
workload_type: "compute"
deployment_guide_provided: false
benchmark_script_provided: false
```

### 执行流程走查

#### Step 1: 环境备份

产出：aarch64 64 核、256G 内存、无 NUMA 跨节点。

#### Step 2: 场景准备（分支 B：未提供材料）

输出执行计划：
- 缺失项：部署指导、测试脚本、指标定义
- 默认假设：使用本地部署，以处理延迟和吞吐为关键指标
- 请用户确认后继续

用户确认 → 继续。

#### Step 3: 目标规格约束

用户未提供目标规格 → 跳过。

#### Step 4-5: 基线测试与确认

用户按计划执行测试，反馈基线结果。

#### Step 6: 瓶颈识别

`cpu_bottleneck` → 继续采集火焰图、热点函数、进程/线程和 topdown 证据。

#### Step 7: 候选优化 skill 列表生成

根据火焰图和 topdown 生成候选顺序：编译 → 性能库 → CPU 亲和性；候选完成后追加 coverage skill：应用配置、网络、OS、BIOS、硬件升级、Other。

**Round 1**：编译器优化

- 用户提供了 hot_functions 数据（图像处理内核函数占比 40%）
- 建议：`-O3 -mcpu=tsv110 -ffast-math`
- 验证：吞吐 +22%
- `iteration_decision: continue`

**Round 2**：性能库选型

- 热点函数包含 memcpy 密集操作
- 建议替换为优化版 memcpy
- 验证：吞吐 +5%
- `iteration_decision: continue`

**Round 3**：CPU 亲和性

- 64 核但进程只用了 32 个线程
- 建议：绑定到单 NUMA 节点
- 验证：吞吐 +3%
- `iteration_decision: stop`（收益递减）

#### Step 9: 报告输出

```
综合收益：吞吐 +31.5%
主要贡献：编译选项（+22%）、memcpy 替换（+5%）、NUMA 绑定（+3%）
停止原因：连续优化收益递减
```
