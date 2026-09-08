# torchrun 训练 Hostbound 场景 CPU 亲和性实战案例

## 场景概述

- **框架/进程**：torchrun 分布式训练（torchtitan-npu / DeepSeek V4 debug 模型），非 vLLM 推理
- **进程结构**：`pt_elastic`(调度母进程) → `torchrun` → 多个 `python3.11` worker（每个 rank 一个）
- **硬件**：鲲鹏 950 (384 逻辑核, 4 NUMA node) + Ascend 950 NPU × 8
- **关键拓扑**：NPU 0-3 → NUMA node0 (CPU 0-95)；NPU 4-7 → NUMA node2 (CPU 192-287)
- **容器**：torchtitan_master_A5，cpuset 0-383（无限制）

## 核心问题：Hostbound

**判定特征**（本案例实测）：
- NPU 利用率低：`Computing` 仅占 step 的 18.5%，`Free`（等 host）占 78.5%
- step 时间分解（msrof step_trace_time）：Computing 231ms / Free 979ms / Stage 1248ms
- 单 step 算子数极多：`operator_details.csv` 中 **380,156 个 op**
- Host/Device 时间比：Host Self 15937ms vs Device Total 1105ms = **14.4x**
- worker 上下文切换高：`pidstat -w` 显示 cswch/s **1082-2157**（>500 细粒度干预阈值）
- worker `task-clock` 极低（perf stat: 1.66-4.2 msec），但 CPU 主线程占用高（60-74%）

**根因**：训练主线程（python3.11）负责**并发下发数十万算子**，是 host 侧串行/并发瓶颈；NPU 计算快（小模型），大部分时间在等 host 下发。

## 关键误区与教训（重要）

### ⚠️ 单核独占热点主线程 → 严重劣化

作为对照组实验，把训练 worker 的主线程（第一热点）**绑定到单个逻辑核并禁止迁移**，结果：
- step 时间从 ~0.75s **劣化到 ~1.85s**（2.5x 回退）
- 原因：hostbound 场景主线程需要**多核并发**下发算子，锁死单核使下发被串行化

**结论**：**"热点主线程绑核越紧越好"的假设在 hostbound 训练中被实测否定**（对照：本 skill 的 vLLM 推理基线也是固定 8 核段而非单核，见 SKILL.md 线程角色绑核表）。必须先用负载类型判定确认是否 hostbound，再决定绑核粒度。

## 有效方案（从好到差，实测数据）

| 方案 | 绑核方式 | 稳态 step | tps | 相对基线 | 结论 |
|------|---------|----------|-----|---------|------|
| **基线** | 无绑核（worker 飘到 node0，NPU 在 node2） | ~0.75-0.80s | ~720 | — | 跨 socket |
| **方案 A** | `taskset` 全程绑 node2 (192-287) 大池 | ~0.713s | ~810 | **-10.6%** | 消除跨 socket |
| **方案 B** | `numactl --cpunodebind=2 --membind=2` | ~0.712s | ~810 | -10.6% | 与 A 等效（host 内存影响小）|
| **方案 D** | 物理核分离（node2 内单 SMT） | ~0.713s | ~810 | -10.6% | 与 A 等效 |
| **方案 E** | 线程角色分组（主/acl/release 分核） | ~0.713s | ~810 | -10.6% | 与 A 等效 |
| ❌ 对照组：主线程单核独占 | 主线程绑单核 | ~1.85s | ~310 | **+147%** | 劣化 |
| **✅ 融合方案 F** | **L3 Cluster 宽松池 + 关 numa_balancing** | **~0.60s** | **~960** | **-25%** | **最优** |

## 最优实践：融合方案 F（推荐）

**NPU 设备对齐 + L3 Cluster 分组 + 宽松池 + 关闭 numa_balancing**：

```bash
# 1. 定位 NPU 所在 NUMA node（Ascend /sys 不可靠，用 npu-smi + lspci 交叉验证）
npu-smi info -m
lspci -d 19e5: -v

# 2. 获取 node 内 L3 Cluster 布局（sysfs 实测，勿照搬文档4核）
for cpu in 192 193 194 195; do
  echo "CPU $cpu -> L3 id $(cat /sys/devices/system/cpu/cpu$cpu/cache/index3/id) shared $(cat /sys/devices/system/cpu/cpu$cpu/cache/index3/shared_cpu_list)"
done

# 3. 关闭 numa_balancing（防线程跨 NUMA 自动迁移，加剧 hostbound 抖动）
echo 0 > /proc/sys/kernel/numa_balancing

# 4. 训练启动后，把每个 worker（comm=python3.11）绑到独立的 L3 Cluster 宽松池
#    worker1 -> L3(192-207)，worker2 -> L3(208-223)  （16 逻辑核/域，宽松不锁单核）
for pid in $(pgrep -f 'torchtitan_npu.entry'); do
  comm=$(cat /proc/$pid/comm 2>/dev/null)
  [ "$comm" != "python3.11" ] && continue
  taskset -pc 192-207 "$pid"
  for tid in $(ls /proc/$pid/task); do taskset -pc 192-207 "$tid"; done
done

# 5. 验证（对比绑核前后 step 时间）
tail -f /tmp/train_run.log
```

**关键点**：
- 先**只做 NPU 设备对齐**（A/B/D/E 都是这一步的收益），稳定 -10.6%
- 再叠加 **L3 Cluster 分组 + 关 numa_balancing**，额外到 -25%（缓存局部性 + 消除迁移）
- **宽松池（每个 L3 域 ≥8 核）**，绝不单核独占下发主线程

## 线程角色识别（hostbound 训练）

与 vLLM 不同，训练 worker 线程名多为框架名（`python3.11`），**无明确 PTA 标识**，需溯源：

| 线程名 | 角色 | CPU 特征 | 处理 |
|--------|------|---------|------|
| `python3.11`(主线程) | 算子下发（第一热点） | 60-74% | **宽松池多核**，非单核独占 |
| `acl_thread` | host↔NPU 控制 | 5-18% | 与主线程同 L3 域 |
| `release_thread` | 资源释放 | ~2% | 同 L3 域 |
| `pt_autograd_*` | 自动求导 | 低 | 辅助池 |
| `hccl_watchdog_t`/`CaffeTaskThread`/`Hccl_HeartBeat` | 驱动监控 | ~0% | 辅助池 |
| `adx_data_dump_t`/`AtraceMonitor`/`PlogFlush` | 日志/监控 | ~0% | 辅助池 |

**未命名/框架名线程**：禁止未经溯源直接套用 PTA 分级绑核取向，先识别热点等级再决定。

## 其他经验

- **HCCL 端口冲突**：容器多进程需设 `HCCL_NPU_SOCKET_PORT_RANGE` 避开默认 16666
- **NPU 占用检查**：绑核前必须 `npu-smi info` 确认目标卡空闲（其他容器可能占用 0-5）
- **seq_len 约束**：deepseek_v4 的 compress_ratio=96，seq_len 需为其倍数（如 576）
- **内存绑定收益小**：模型数据驻留 NPU HBM，host 内存绑定（方案 B）对 hostbound 训练额外收益 <0.5%，可选

## 参考

- SKILL.md 中 `## Cluster / Die / L3 拓扑分析` 与 `## 细粒度线程模型` 章节
- `references/ascend-vllm-binding.md`（对照 vLLM 场景的差异）
