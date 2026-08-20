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
scripts/backup_environment.sh ./output/env_backup
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
scripts/init_report.sh ./output/report "mysql-8u32g-readonly"
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

最终瓶颈重新分类为 `cpu_bottleneck`。根据 `performance_signal_summary.json` 生成候选顺序：CPU 亲和性、应用配置、性能库；候选完成后追加 coverage skill：网络、编译、OS、BIOS、硬件升级、Other。

#### Step 8: 迭代优化

**Round 1**：CPU 亲和性优化

- `cpu-affinity-optimization` 强制首位候选（signal-threshold-exempt）
- MySQL 进程绑到 NIC 所在 NUMA node，IRQ 迁移避让应用核
- 验证：TPS 3200 → 3210（+0.3%，噪声区，保留但记录）
- `iteration_decision: continue`

**Round 2**：应用配置优化

- `application-config-optimization` 触发数据库专项
- AHI 分析：`mysqld_cpu_pct=92, threads_per_core=8, buffer_pool_hit_rate=99.5%`
- 建议：关闭 AHI（`innodb_adaptive_hash_index=OFF`）
- 验证：TPS 3210 → 3650（+13.7%）
- `iteration_decision: continue`

**Round 3**：性能库选型

- 检测到 jemalloc 可用
- 建议：`LD_PRELOAD=/usr/lib64/libjemalloc.so.2`
- 验证：TPS 3650 → 3731（+2.2%）
- `iteration_decision: continue`

**Round 4**：OS 优化

- 建议：`sysctl -w vm.swappiness=1`、关闭 THP
- 验证：TPS 3731 → 3740（+0.2%，< 1%）
- `iteration_decision: continue`

**Round 5**：编译器优化

- 当前 GCC 版本 10.3，建议开启 `-O3 -mcpu=tsv110`
- 但用户表示无法重编译 → 跳过
- `iteration_decision: stop`

#### Step 9: 报告输出

```
综合收益：TPS 3200 → 3740（+16.9%）
主要贡献：AHI 关闭（+14.1%）、jemalloc（+2.2%）、OS 调优（+0.2%）
停止原因：OS skill 推荐已验证完毕，但单 skill 需验证完全 subskill 给出的所有推荐；全局停止来自剩余 high/medium 动作无法执行（编译优化被用户拒绝）且用户要求输出报告
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

根据火焰图和 topdown 生成候选顺序：CPU 亲和性 → 编译 → 性能库；候选完成后追加 coverage skill：应用配置、网络、OS、BIOS、硬件升级、Other。

#### Step 8: 迭代优化

**Round 1**：CPU 亲和性

- 64 核但进程只用了 32 个线程
- 建议：绑定到单 NUMA 节点
- 验证：吞吐 +3%
- `iteration_decision: continue`

**Round 2**：编译器优化

- 用户提供了 hot_functions 数据（图像处理内核函数占比 40%）
- 建议：`-O3 -mcpu=tsv110 -ffast-math`
- 验证：吞吐 +22%
- `iteration_decision: continue`

**Round 3**：性能库选型

- 热点函数包含 memcpy 密集操作
- 建议替换为优化版 memcpy
- 验证：吞吐 +5%
- `iteration_decision: stop`（收益递减）

#### Step 9: 报告输出

```
综合收益：吞吐 +31.5%
主要贡献：编译选项（+22%）、memcpy 替换（+5%）、NUMA 绑定（+3%）
停止原因：连续优化收益递减
```

---

## 示例 3：Ascend910 NPU + vLLM 推理优化

### 场景输入

```
scenario_description: "Ascend910 单卡 vLLM 推理优化，qwen2.5-1.5b，单流吞吐导向"
application_name: "vllm-ascend"
workload_type: "ai_inference"
inference_framework: "vllm"
device_type: "npu"
model_name: "qwen2.5-1.5b"
model_path: "/data/models/qwen2.5-1.5b"
tokenizer_path: "/data/models/qwen2.5-1.5b"
npu_device_ids: "0"
tensor_parallel_size: 1
benchmark_tool: "vllm_benchmark"
target_metrics:
  target_tps: 100
deployment_topology: "单机单卡，容器内运行"
benchmark_script_provided: true
deployment_guide_provided: true
change_scope: "app_config,library,compiler,os,cpu_affinity"
restart_allowed: true
rebuild_allowed: true
```

### 平台与环境

| 项目 | 规格 |
| --- | --- |
| CPU | Kunpeng 920（aarch64，4 NUMA node × 160 核 = 640 逻辑核；容器内可见全核，绑核使用 NUMA node 3 的 480-503） |
| 内存 | 256G DDR4 |
| NPU | Ascend 910 单卡 |
| OS | openEuler 24.03 |
| 加速栈 | CANN + torch_npu + vLLM-Ascend |
| Python | BiSheng Python 3.11（可重编译） |
| 运行形态 | Docker 容器内运行 vLLM 服务 |

### 执行流程走查

#### Step 1: 环境备份

```bash
scripts/backup_environment.sh ./output/env_backup
```

产出：Kunpeng 920 4 个 NUMA 节点 × 160 核 = 640 逻辑核（容器内可见全核，绑核使用 NUMA node 3 的 480-503）、内存 256G、Ascend 910 单卡、容器内 CANN/torch_npu 版本确认、BiSheng Python 工具链可用。

#### Step 2: 场景准备

用户已提供 vLLM-Ascend 部署指导和 benchmark 脚本（vllm_benchmark 单流模式，固定 prompt 长度）。

#### Step 3: 目标规格约束

未提供目标规格 → 使用整机规格约束：CPU 全核可用、NPU 单卡、容器内运行。

#### Step 4-5: 基线测试与确认

```bash
scripts/init_report.sh ./output/report "vllm-ascend-qwen15b"
scripts/run_placeholder_benchmark.sh "vllm-ascend-qwen15b"
```

**S0 基线**：

| 指标 | 值 |
| --- | --- |
| 吞吐（tok/s） | 64.63 |
| 线程总数 | 826 |
| OMP_NUM_THREADS | 82 |
| NPU 利用率 | ~78% |
| CPU 利用率（峰值核） | ~95% |
| TTFT（首 token 延迟） | 偏高 |

用户确认基线 → 继续。

#### Step 6: 瓶颈识别

调用 `io-memory-network-bottleneck-analysis` 与 `accelerator-optimization`：

- NPU 利用率 78% 未饱和 → 排除纯 NPU 算力瓶颈
- CPU 峰值核 95%、826 线程跨 NUMA 散布 → CPU 调度与 NUMA 跨节点访问为主瓶颈
- 信号归档至 `performance_signal_summary.json`，主分类 `cpu_bottleneck`，次分类 `gpu_npu_bottleneck`（host feed 不足）

#### Step 7: 候选优化 skill 列表生成

候选顺序（evidence_candidate）：CPU 亲和性 → 应用配置 → 性能库 → 编译；候选完成后追加 coverage skill：OS、BIOS、网络、硬件升级、Other。

#### Step 8: 迭代优化

##### Round 1：CPU 亲和性（C5 线程级 NUMA3 绑核）

- 调用 `cpu-affinity-optimization`，按 vLLM 三类线程分组绑核：
  - EngineCore 线程 → 逻辑核 480-487
  - acl/CANN 通信线程 → 逻辑核 488-495
  - release/GIL 释放线程 → 逻辑核 496-503
- 验证：64.63 → 70.14 tok/s（+8.52%）
- `iteration_decision: continue`

##### Round 2：应用配置（C8）

- 调用 `application-config-optimization`，针对 vLLM-Ascend 调参：
  - `VLLM_ENGINE_ITERATION_INTERVAL=...` 相关 → TQE=2（Tensor Queue Entries）→ 吞吐 +30.2%
  - `OMP_NUM_THREADS=8`（从 82 下调）→ TTFT -68%，吞吐持平
  - `gc:0.95` + `split:50`（内存分配与切分参数）→ +3.2%
- 验证：70.14 → 95.32 tok/s（本轮 +35.9%，累计 +47.5%）
- `iteration_decision: continue`

##### Round 3：性能库（S5 tcmalloc）

- 调用 `performance-library-selection`，热点函数含 malloc/free 密集
- 选型：BiSheng tcmalloc 4.5.18，`LD_PRELOAD=/usr/local/lib/libtcmalloc.so`
- 陷阱：容器内 `libarcher`（GCC OpenMP runtime shim）会覆盖 LD_PRELOAD 链，需先 `unset` 或显式排除
- 验证：95.32 → 104.17 tok/s（本轮 +9.3%，累计 +61.2%）
- `iteration_decision: continue`

##### Round 4：编译（C6 Python PGO）

- 调用 `compiler-optimization`，对 BiSheng Python 3.11 应用 B006 LTO+PGO
- PGO 训练集：vLLM 启动 + benchmark 单流负载采样
- 验证：104.17 → 110.92 tok/s（本轮 +6.5%，累计 +71.6%）
- `iteration_decision: continue`

##### Round 5：编译深化（C7 全 PGO + tcmalloc）

- 对 PyTorch + torch_npu 应用 ThinLTO + PGO（与 C6 Python PGO 共用 profile 体系）
- 叠加 S5 tcmalloc LD_PRELOAD 保持生效
- 验证：110.92 → 125.71 tok/s（本轮 +13.3%，累计 +94.5%）
- `iteration_decision: continue`

##### 被拒绝项

| 候选动作 | 验证结果 | 拒绝理由 |
| --- | --- | --- |
| 纯 ThinLTO 无 PGO | -22.4% | 缺少 profile 引导，代码布局退化 |
| 通用 profile（非 vLLM 采样） | -1.33% | 训练集与实际负载不匹配 |
| block-size=16 | 噪声级波动 | NPU 算子块大小对本模型无收益 |
| chunked-prefill | 单流有害 | chunked-prefill 面向多流并发，单流场景引入额外调度开销 |

#### Step 9: 报告输出

##### 最终配置表

| 类别 | 配置项 | 值 |
| --- | --- | --- |
| CPU 亲和性 | EngineCore 核 | 480-487 |
| CPU 亲和性 | acl/CANN 核 | 488-495 |
| CPU 亲和性 | release 核 | 496-503 |
| 应用配置 | TQE | 2 |
| 应用配置 | OMP_NUM_THREADS | 8 |
| 应用配置 | gc | 0.95 |
| 应用配置 | split | 50 |
| 性能库 | LD_PRELOAD | /usr/local/lib/libtcmalloc.so（BiSheng 4.5.18） |
| 性能库 | libarcher 排除 | unset / 显式屏蔽 |
| 编译 | BiSheng Python | B006 LTO+PGO |
| 编译 | PyTorch | ThinLTO + PGO |
| 编译 | torch_npu | ThinLTO + PGO |
| 启动 | vllm serve | --model qwen2.5-1.5b --device npu --tensor-parallel-size 1 |

##### 收益分解表

| 阶段 | 吞吐（tok/s） | 本轮增量 | 累计收益 | 归因方法 |
| --- | --- | --- | --- | --- |
| S0 基线 | 64.63 | — | — | — |
| C5 NUMA3 绑核 | 70.14 | +8.52% | +8.52% | `single_variable_round` |
| C8 应用配置 | 95.32 | +35.9% | +47.5% | `single_variable_round`（TQE/OMP/gc/split 逐项） |
| S5 tcmalloc | 104.17 | +9.3% | +61.2% | `single_variable_round` |
| C6 Python PGO | 110.92 | +6.5% | +71.6% | `single_variable_round`（B006 LTO+PGO vs 纯 LTO 对照） |
| C7 全 PGO+tcmalloc | 125.71 | +13.3% | +94.5% | `single_variable_round`（PyTorch+torch_npu ThinLTO+PGO 叠加验证） |

```
综合收益：64.63 → 125.71 tok/s（+94.5%）
主要贡献：C8 应用配置（+47.5% 累计基础）、C7 全 PGO（+13.3% 本轮）、S5 tcmalloc（+9.3% 本轮）
停止原因：候选 skill 全部完成，剩余 coverage skill（OS/BIOS/网络）无对应瓶颈证据
风险：tcmalloc LD_PRELOAD 需在容器启动脚本固化；PGO profile 与负载强绑定，换模型需重新训练
回退：还原 OMP_NUM_THREADS、unset LD_PRELOAD、回退 BiSheng Python 与 torch_npu 至非 PGO 版本
```
