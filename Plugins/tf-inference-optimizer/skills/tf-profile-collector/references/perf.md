# perf / topdown / objdump / 多线程采集（按需深采）

trace 只覆盖「op 级耗时」一个维度。以下手段覆盖其余多个维度（调度 / 亲和性 / 内存分配 / 内存连续性 / 内存带宽 / 计算效率），按采集计划命中信号后定向深采。内存布局/分配规划维度见 `trace.md` 第 6 节。

## 1. perf stat（IPC / cache / branch / 迁移）

> ⚠️ **事件名统一用 aarch64（ARM PMU）风格**，不用 x86 的 `LLC-load-misses` 等名字。不同 SoC 事件名有差异，采集前用 `perf list` 确认本平台事件名。

```bash
# 附加到运行中的 server 进程（预测型服务，先找到 pid）
pid=$(pgrep -f predictor_server)
perf stat -p $pid -e CPU_CYCLES,INST_RETIRED, \
  L1D_CACHE_REFILL,L1I_CACHE_REFILL,L2D_CACHE_REFILL, \
  BR_MIS_PRED,context-switches,cpu-migrations -- sleep 30
```

| 指标（aarch64 事件） | 对应维度 | 判断 |
|---|---|---|
| IPC (INST_RETIRED/CPU_CYCLES) | Op 计算效率 | < 1 说明计算未充分利用，可能 kernel 实现差或访存受限 |
| L1D_CACHE_REFILL | 内存连续性 | 高 → AoS/访存模式问题 |
| L1I_CACHE_REFILL | 调度 / 代码布局 | 高 → 代码布局差，考虑 PGO/LTO |
| L2D_CACHE_REFILL | 内存连续性 / 带宽 | 高 → 数据不连续或带宽受限 |
| BR_MIS_PRED | Op 计算效率 | 高 → 分支密集，可考虑分支消除 |
| context-switches | CPU 亲和性 / 调度 | 高 → 线程竞争或绑核缺失 |
| cpu-migrations | CPU 亲和性 | 高 → 未绑核，跨 NUMA 迁移 |

> `L1D_CACHE_REFILL` 等 cache refill 事件按核计数、按需换算（`refill × cacheline` 得字节量），判断是否带宽受限要结合第 7 节内存带宽分析。

## 2. perf record（火焰图 / 内存分配）

```bash
# 火焰图：CPU 采样热点函数
perf record -p $pid -g -o perf.data -- sleep 30
perf report -i perf.data --stdio

# 内存分配热点（malloc/memcpy 占比）
perf record -p $pid -e sched:sched_switch,kmem:mm_page_alloc -- sleep 30
```

判断：malloc/memcpy 占比高 → 内存分配优化维度（减少拷贝、复用 buffer）。

## 3. topdown（L1 / L2 / 末级 cache miss，FE / BE bound）

```bash
# 各级 cache miss（aarch64 事件名，先 perf list 确认本平台名字）
perf stat -p $pid -e L1I_CACHE_REFILL,L1D_CACHE_REFILL,L2D_CACHE_REFILL,LL_CACHE_MISS -- sleep 30
```

| 信号 | 维度 | 含义 |
|---|---|---|
| L1I_CACHE_REFILL 高 | Op 计算效率 / 调度 | 代码布局差，考虑 PGO/LTO |
| L2D_CACHE_REFILL / LL_CACHE_MISS 高 | 内存连续性 / 亲和性 / 带宽 | 数据不连续、跨 NUMA 或带宽受限 |

## 4. objdump 反汇编（指令集使用）

```bash
# 检查目标 op kernel 是否命中 NEON/SVE 指令（当前平台最优实现）
objdump -d <BIN_SERVER> | grep -c -E "fmul|fadd|ld1|st1"      # NEON
objdump -d <BIN_SERVER> | grep -c -E "whilelo|fcmla"            # SVE
```

判断：目标 op kernel 无 NEON/SVE 指令 → 未用平台最优实现，存在 kernel 优化空间。

## 5. 多线程 / 调度分析

```bash
# 线程分布与上下文切换
ps -T -p $pid | wc -l                          # 线程数
pidstat -w -p $pid 1 10                        # 上下文切换率
cat /proc/$pid/status | grep -E "Threads|voluntary_ctxt" 
```

结合 trace timeline 观察：
- inter-op / intra-op 并发是否充分（Q/K/V 等 sibling 是否真并行）。
- 大量小 op 是否在同一调度线程后排队（dispatch 瓶颈）。

## 6. 按需深采路由

| 静态图/trace 命中信号 | 定向深采 |
|---|---|
| 大量独立小 op + dispatch 高 | trace timeline + 多线程 |
| 线程切换高 / 迁移核高 | perf context-switch / cpu-migrations |
| malloc/memcpy 占比高 | perf record 分配采样 |
| cache miss 高 | perf stat L1D_CACHE_REFILL/L2D_CACHE_REFILL |
| IPC 低 / 某 op 耗时长 | objdump + perf stat IPC |
| 算术强度低 / IPC 正常但耗时长 | 内存带宽分析（第 7 节 roofline） |

## 7. 内存带宽瓶颈分析（判断是否访存受限）

GEMM 类热点在 IPC 正常但耗时偏高时，瓶颈往往是**内存带宽**而非计算。带宽是 IPC/cache-miss 无法直接回答的维度，需单独测：

### 7.1 DRAM/总线带宽（平台相关）

```bash
# 方法一：perf 平台总线事件（Kunpeng/海思 SoC 有 hisi_l3c / bus 事件，先 perf list 确认）
perf list | grep -iE "bus|dram|mem|bandwidth|l3c"
perf stat -p $pid -e <平台总线/内存访问事件> -- sleep 30

# 方法二：独立带宽基准（STREAM 型）测当前环境可达到的带宽上限
# 用 likwid-bench / STREAM / numactl 基准，得到本 NUMA 节点的实测带宽
```

### 7.2 从事件估算带宽受限

- 计算热点 op 的**算术强度**（FLOP / byte）：`静态 FLOPs（graph_profile.json）÷ 访存字节（refill × cacheline + 权重重读）`。
- 对比本平台 roofline（实测带宽 × 峰值算力）：算术强度落在带宽受限区 → 优化方向是「减少访存 / 复用 / 布局连续化」，而不是继续提高计算效率。

### 7.3 判断信号

| 信号 | 含义 |
|---|---|
| 带宽占用接近实测上限（>80%） | 带宽受限，优化「数据布局/复用」而非「计算」 |
| L2D_CACHE_REFILL 高但 IPC 正常 | 访存不足，缺缓存复用或数据不连续 |
| NUMA remote 访问占比高 | 跨 NUMA 带宽打折扣，应绑核到本地 NUMA |
