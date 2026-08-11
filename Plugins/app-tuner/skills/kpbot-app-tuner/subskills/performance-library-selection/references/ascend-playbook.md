# Ascend NPU 场景经验 Playbook

本 playbook 沉淀 Ascend NPU（PyTorch + CANN + torch_npu）场景下性能库替换的经验线索与推荐策略。经验可直接用于推荐结论生成（标注 `confidence: experience_only`），现场 perf 证据优先。以下经验来自历史 Qwen3-8B 推理进程（python3 + CANN 9.0.0 + torch_npu）的 perf 采集。

## 经验线索

### 1. CANN runtime 内部锁竞争可能掩盖 malloc 热点

历史 perf 采集显示 `libpthread-2.31.so` 占 33.94%（`__pthread_rwlock_unlock` 14.52% + `__pthread_mutex_lock` 7.50%），而 malloc 热点仅 1.60%。但 CANN runtime（`libruntime_v100.so`）的 `std::_Rb_tree<...mutex...>::erase`（5.83%）和 `std::vector::_M_realloc_insert` 等容器操作隐含大量 malloc/free，这些被归入 CANN DSO 而非 libc malloc。

**经验推荐**：Ascend 场景即使 libc malloc 热点 <2%，若检测到 CANN runtime 库加载且 DSO 占比高，仍推荐 tcmalloc（CANN 内部隐含 malloc 可能受益，标注 `confidence: experience_only` 或 `low`，取决于 CANN DSO 占比证据），主框架执行阶段可按验证流程设计量化收益。

### 2. LD_PRELOAD 叠加约束

Ascend 推理/训练进程常已设置 `LD_PRELOAD=libgomp.so.1`（OpenMP），新增任何库（tcmalloc/jemalloc/stringlib）的 LD_PRELOAD 必须追加而非覆盖，否则破坏 OpenMP 并行。

### 3. 容器场景路径可见性

CANN 部署在容器内（如 `/home/developer/Ascend/cann-9.0.0/`），所有 LD_PRELOAD 的 .so 必须在容器内可见，宿主机路径不可用。

### 4. NPU Caching Allocator 不受 host 侧 malloc 库影响

torch_npu 的 `c10_npu::NPUCachingAllocator` 管理 NPU 显存，tcmalloc/jemalloc 只优化 host 侧 malloc。

**经验推荐**：若现场证据显示瓶颈在 NPU 显存分配（host 侧 malloc 热点极低且无 CANN DSO 高占比），host 侧库替换可直接 `not_recommended`（依据：经验显示收益微小），无需强制实测。

## Ascend NPU 场景推荐策略

按证据+经验推荐候选库，主框架执行阶段可按验证流程设计量化收益：

| 候选库 | 推荐策略 | 经验优先级 | 验证流程设计建议 |
|--------|----------|------------|------------------|
| tcmalloc | Ascend 场景优先推荐（CANN 内部隐含 malloc 可能受益） | 高 | 建议主框架优先验证以量化 host 侧收益 |
| jemalloc | 次优先推荐（多线程 arena 管理） | 中 | 可选由主框架验证 |
| bisheng-stringlib | aarch64 + memcpy 相关 DSO 加载时推荐 | 中（条件推荐） | 可选由主框架验证 |

## 历史实测收益基线

以下为 Qwen3-8B BF16 单卡推理（ge_graph 图模式）场景的 tcmalloc LD_PRELOAD 实测数据，作为 `expected_gain` 填写参照。实测环境：Atlas A3 910C / aarch64 40 核 / CANN 9.0.0 / torch_npu 2.8.0.post4 / 已有 `LD_PRELOAD=libgomp.so.1`。

| 指标 | 基线中位数 | tcmalloc 中位数（2 次） | 收益 | 说明 |
|------|-----------|------------------------|------|------|
| Prefill（首 token 耗时） | 36.10 ms | 35.14 ms | **-2.66%** | host 侧内存分配受益，正向 |
| Decode（每步耗时） | 15.47 ms | 15.46 ms | -0.06% | 可忽略，NPU 计算瓶颈不受 host malloc 影响（见经验#4） |

采集证据：malloc 热点 3.14%（中证据）、memcpy 0.68%、CANN DSO 合计 1.06%、pthread 锁竞争 1.62%。注意：本次 CANN DSO 占比远低于下方补充信号阈值（>10%），未触发二次归因；收益主要来自 host 侧 malloc 路径优化。该收益区间仅适用于相近配置，作为 `expected_gain` 估计而非保证值。

## Ascend 场景 tcmalloc 判断补充信号

进程加载 `libruntime_v100.so` / `libtorch_npu.so` / `libhccl.so` / `libascendcl.so`，且 perf 热点 DSO 排名中 CANN/torch_npu 库合计占比 > 10% 时（即使 malloc 热点未达 2%，CANN 内部 `std::vector::_M_realloc_insert` / `std::_Rb_tree::erase` 等容器操作隐含大量 malloc/free 调用），应单独提取 CANN DSO 内的容器操作热点做二次归因。

## Ascend NPU 推理 Host 侧 Malloc 优化

> 本章针对 `ascend_runtime` 类别在 vLLM/推理框架下的 host 侧 malloc 路径。权威门控见 SKILL.md"安全与架构红线 #3"：必须有本轮 `current_run_id` 的现场 perf 采集成功才能输出推荐结论。

### 背景：NPU 推理的 host 侧 malloc 路径

vLLM 在 Ascend NPU（torch_npu）上跑推理时，device 侧显存由 NPUAllocator 管理，但 **host 侧仍有大量 malloc 调用**：

- Python tensor 创建/destroy 走 Python heap 与 CPython 小对象分配器，底层仍依赖 glibc malloc
- torch_npu 的 host-device 拷贝 staging buffer、KV cache 的 metadata、调度器对象均在 host 侧分配
- CANN runtime（libruntime_v100/libascendcl）内部容器（`std::vector`/`std::_Rb_tree`/`std::_Hashtable`）的元素分配走 host malloc，高并发场景下锁竞争显著

### glibc malloc 在高并发推理的瓶颈

- 单一 arena + 锁：多线程 acquire 信号量竞争，热点表现为 `__pthread_mutex_lock` / `__lll_lock_wait`
- 碎片化：频繁的 tensor 创建/释放导致 arena 碎片，RSS 持续上升
- 无大页支持：4KB page → TLB miss 高

### 替换方案对比

#### tcmalloc 优势

- **线程缓存**：每线程独立 ThreadCache，小对象无锁分配，消除 `__pthread_mutex_lock` 热点
- **小对象分配效率高**：size-class 化的 free list，O(1) 分配
- **BiSheng tcmalloc 特有**：与 BiSheng 编译器运行时库（libomp/libarcher）协同，OpenMP 场景下避免运行时冲突
- **环境变量调优**：
  - `TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES`：增大线程缓存总容量（默认 8MB，高并发推理建议 64-128MB）
  - `TCMALLOC_AGGRESSIVE_DECOMMIT`：空闲内存归还 OS，控制 RSS
  - `TCMALLOC_SAMPLE_PARAMETER`：采样频率，调试用

#### jemalloc 优势

- **大页支持**：2MB huge page backend，减少 TLB miss，适合内存带宽密集场景
- **arena 分离**：默认每 CPU 一个 arena，减少锁竞争
- **碎片控制**：extent 复用策略优于 glibc
- **环境变量调优**（`MALLOC_CONF`）：
  - `narenas`：arena 数量
  - `lg_dirty_mult`：dirty page decay
  - `thp:always`：强制透明大页

### LD_PRELOAD 注入方式

```bash
# 基础注入（不重编译）
export LD_PRELOAD=/path/to/libtcmalloc.so
python -m vllm.entrypoints.openai.api_server --model qwen2.5-1.5b --device npu

# 验证加载成功
grep tcmalloc /proc/$(pgrep -f vllm)/maps
# 预期输出包含 libtcmalloc.so 路径
```

### 实战收益表（Ascend910 + vLLM qwen2.5-1.5b，仅作参照，不作判定依据）

| 分配器 | 吞吐 (tok/s) | vs S0 基线 | vs glibc | 说明 |
|--------|-------------|-----------|---------|------|
| glibc malloc (S0 基线) | 64.58 | — | — | 默认 |
| BiSheng tcmalloc 4.5.18 (C7) | 104.17 | +61.2% | +61.2% | LD_PRELOAD 注入，含 PGO 协同 |
| jemalloc 2MB 大页 (C7 备选) | 120.58 | +86.5% | +86.5% | 比 tcmalloc 低 4.26% |

> 上表为历史实测基线，**仅作参照**。现场判定必须基于本轮 `current_run_id` 的 perf 采集。tcmalloc 与 jemalloc 的相对优劣需在同一 PGO/编译环境下实测对比，不可跨环境外推。

### 验证流程设计要点

1. 基线：纯 glibc malloc，跑 ≥3 次基准取中位数
2. 单变量：仅替换 malloc，其他环境变量、batch size、并发数保持不变
3. 加载验证：`grep tcmalloc /proc/<pid>/maps` 确认 .so 已加载
4. 中位数对比：替换后 ≥3 次基准取中位数，计算增量
5. 回退：`unset LD_PRELOAD` 后重跑确认回归基线

## tcmalloc 运行时依赖陷阱

> 实战踩坑经验，来自 Ascend910 + vLLM 优化案例。本章为诊断指引，不输出 `candidate_actions`。

### 问题：libarcher TSAN 依赖导致死锁

tcmalloc 在 Ascend 场景下有一个隐蔽的运行时依赖陷阱：

- 系统 `/usr/lib64/libarcher.so`（OpenMP runtime 的 TSAN 桥接库）有 ThreadSanitizer 依赖
- tcmalloc 与 TSAN 的拦截器冲突 → 进程启动后卡死或随机死锁
- 表现：`LD_PRELOAD=libtcmalloc.so python -m vllm ...` 启动后无响应、`top` 显示进程 D 状态

### 根因：BiSheng libomp vs 系统 libarcher

BiSheng tcmalloc 编译时链接 BiSheng compiler 的 libomp，运行时需加载 BiSheng 的 libarcher（无 TSAN 依赖）。若 `LD_LIBRARY_PATH` 未优先指向 BiSheng compiler lib path，动态链接器会先找到系统 `/usr/lib64/libarcher.so`，触发死锁。

### 诊断方法

```bash
# 1. 检查 python 链接的 archer 库来源
ldd $(which python) | grep archer
# 错误输出：libarcher.so.1 => /usr/lib64/libarcher.so.1  ← 系统库，有 TSAN 依赖
# 正确输出：libarcher.so.1 => /path/to/BiShengCompiler/lib/libarcher.so.1

# 2. strace 追踪 archer 库加载路径
strace -f -e openat python -c "pass" 2>&1 | grep archer
# 观察 openat() 实际打开的路径，确认是否为 BiSheng compiler lib

# 3. 检查当前 LD_LIBRARY_PATH
echo $LD_LIBRARY_PATH
# 必须包含 BiSheng compiler lib path 且排在 /usr/lib64 之前
```

### 正确配置

```bash
# BiSheng compiler lib path 必须前置
export LD_LIBRARY_PATH=/path/to/BiShengCompiler/lib:$LD_LIBRARY_PATH
export LD_PRELOAD=/path/to/BiShengCompiler/lib/libtcmalloc.so

# 验证依赖解析顺序
ldd $(which python) | grep -E 'archer|libomp'
# 确认 archer 和 libomp 都指向 BiShengCompiler/lib
```

### 候选动作 rollback 要求

涉及 BiSheng tcmalloc 的候选动作，`rollback` 必须包含：

1. `unset LD_PRELOAD`（移除 tcmalloc 注入）
2. 恢复 `LD_LIBRARY_PATH` 原值（移除 BiSheng compiler lib 前置）
3. 目标实例身份复核（确认 PID/进程名回归基线启动方式）

## vLLM Caching Allocator 交互

> 本章针对 vLLM + torch_npu 的两层 allocator 交互。device 侧 allocator 调优不属 `ascend_runtime` 库替换类别，作为协同调优上下文输出。

### 两层 Allocator 架构

vLLM 在 NPU 上运行时存在两层内存管理：

| 层 | 管理对象 | 实现 | 调优入口 |
|----|---------|------|---------|
| L1: vLLM caching allocator | KV cache 的 PagedAttention block | vLLM 内部 | `--gpu-memory-utilization`, `block_size` |
| L2: torch_npu NPUAllocator | device 显存（model weights, activations, KV cache pool） | torch_npu | `PYTORCH_NPU_ALLOC_CONF` |

### 潜在冲突

- L2 的 garbage collection 可能回收 L1 正在引用的 KV cache block → 触发不必要的 device 内存重分配
- L2 的 split 策略与 L1 的 paged block 大小不匹配 → 显存碎片
- 两层独立的 OOM 判定逻辑 → 边界条件下行为不一致

### PYTORCH_NPU_ALLOC_CONF 调优参数

| 参数 | 作用 | 推荐值（推理） | 实战收益 |
|------|------|--------------|---------|
| `garbage_collection_threshold` | 触发 GC 的显存占用阈值 | `0.95` | 提高阈值减少 GC 频率，避免回收活跃 block |
| `max_split_size_mb` | 单次分配最大可拆分 block 大小 | `50` | 限制拆分减少碎片 |

### 实战配置（Ascend910 + vLLM qwen2.5-1.5b）

```bash
export PYTORCH_NPU_ALLOC_CONF=garbage_collection_threshold:0.95,max_split_size_mb:50
python -m vllm.entrypoints.openai.api_server \
  --model qwen2.5-1.5b \
  --device npu \
  --gpu-memory-utilization 0.9 \
  --block-size 16
```

### 协同收益（仅作参照）

| 配置 | 吞吐 (tok/s) | vs 上一轮 | 说明 |
|------|-------------|----------|------|
| tcmalloc only (C7) | 104.17 | — | host 侧 malloc 优化 |
| tcmalloc + PYTORCH_NPU_ALLOC_CONF 调优 | ~107.5 | +3.2% | device 侧 allocator 协同 |

> 历史实测参照，不作判定依据。device 侧调优属协同上下文，不纳入本子 skill 的 `candidate_actions`（库替换范畴）；作为配套建议输出供主框架执行阶段协同落地。
