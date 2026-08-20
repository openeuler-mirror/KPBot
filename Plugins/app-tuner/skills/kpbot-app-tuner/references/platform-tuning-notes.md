# 平台专项调优说明 / Platform Tuning Notes

## NUMA

多插槽服务器必须检查：

- NUMA 拓扑
- CPU 与内存节点分布
- 线程和中断是否跨节点漂移
- 远端内存访问代价

## THP

建议显式检查 THP 状态。

数据库场景下，`THP=always` 往往是需要重点确认的风险项，应结合业务特点判断是否需要关闭或改为 `madvise`。

## HugePages

HugePages 更适合以下场景：

- 内存带宽或 TLB 压力明显
- 大页映射能够显著减少页表开销

纯 CPU-bound 场景下，HugePages 不一定带来明显收益，应避免默认假设其有效。

## ARM / aarch64

ARM / aarch64 平台需额外关注：

- 编译目标架构参数
- 不同平台的计数器和采样质量差异
- perf 证据边界
- 构建系统是否正确打开目标平台优化

若目标平台为 Kunpeng 或其他 ARM64 服务器，应在编译器优化与性能结论中显式注明平台背景。

## Ascend NPU 拓扑

Ascend 系列是华为自研 NPU，在 A+K（Ascend NPU + Kunpeng CPU）推理场景中是核心算力载体。理解其芯片拓扑、HBM 与 DDR 访问路径、HCCS 互联，是 NUMA 亲和性与数据传输优化的前提。

### 910 / 910B / 910C 型号对比

| 型号 | HBM 容量 | FP16 算力 | HBM 带宽 | 主要场景 |
|------|----------|-----------|----------|----------|
| 910   | 32 GB HBM2 | 256 TFLOPS | ~1.2 TB/s | 训练 / 推理 |
| 910B  | 64 GB HBM2e | 376 TFLOPS | ~1.6 TB/s | 大模型推理 |
| 910C  | 128 GB HBM3 | ~800 TFLOPS | ~3.2 TB/s | 超大模型 / 长上下文 |

数字取自公开数据手册，调优现场以 `npu-smi info -t board` 实测为准。

### chip / die 结构

- 每颗 Ascend 910 系列芯片包含多个 AI Core，每个 AI Core 有独立的 Cube/Vector 单元。
- 多 die 封装（910B 及以后）需注意 die 间 HBM 访问延迟差异，跨 die 访问 HBM 比本地 die 高出显著比例。
- `lspci -d 19e5: -vvv` 可看到每张 NPU 卡的 PCI BDF、NUMA node、BAR 空间，是判断 die 归属与 NUMA 亲和性的第一步。

### HCCS 互联

- HCCS（Huawei Cache Coherent System）是 Ascend 卡间高速互联，单链带宽数十 GB/s、延迟 us 级。
- 多卡 TP/PP 通信走 HCCS；拓扑不优（跨 HCCS 域、跨 socket）会显著抬升 HCCL allreduce 延迟。
- 实战经验：vLLM-Ascend 多卡 TP 场景下，应保证参与 TP 的 NPU 卡位于同一 HCCS 域内，并让协调进程绑定到同 NUMA 节点的 CPU。

### NPU NUMA 亲和性

查询 NPU 归属 NUMA node：

```bash
lspci -d 19e5: -v | grep -i numa
# 或
for dev in $(lspci -d 19e5: | awk '{print $1}'); do
  echo "$dev -> NUMA $(cat /sys/bus/pci/devices/$(echo $dev | tr ':' '.')/numa_node)"
done
```

NPU 与其所在 NUMA node 的 CPU 进行 H2D/D2H 拷贝时延迟最低。若协调进程（vLLM worker、tokenizer、DataLoader）被绑到远端 NUMA，HBM 控制延迟和 PCIe DMA 都会抬升。

### HBM vs DDR 访问路径

| 路径 | 介质 | 带宽 | 延迟 | 用途 |
|------|------|------|------|------|
| NPU Core ↔ HBM | device 侧 HBM | TB/s 级 | ns 级 | 权重、KV cache、激活 |
| NPU Core ↔ host DDR | 跨 PCIe | 数十 GB/s | us 级 | H2D/D2H、unified memory |
| CPU ↔ host DDR | host 内存 | 数百 GB/s | ns 级 | tokenizer、feed、调度 |

权重和 KV cache 必须驻留 HBM；任何不必要的 host↔device 往返都是优化对象。

## CANN / torch_npu 平台特性

### CANN 与 torch_npu 版本兼容性

| CANN | torch_npu | PyTorch | 备注 |
|------|-----------|---------|------|
| 7.0 – 7.5 | 2.1.0.x | 2.1.0 | 早期 LTS，910 单卡推理常见 |
| 8.0.RC1 | 2.2.0.x | 2.2.0 | 引入新算子、HCCL 优化 |
| 8.0.RC3 | 2.3.0.x | 2.3.0 | vLLM-Ascend 主流组合 |
| 8.1.RC1 | 2.4.0.x | 2.4.0 | 长上下文改进 |

升级前必须核对 CANN ↔ torch_npu ↔ PyTorch 三元版本矩阵，错配通常表现为算子找不到或段错误而非显式报错。

### 已知问题

- **算子 fallback**：未在 CANN 黑名单/白名单中命中时，torch_npu 会把算子 fallback 到 CPU 执行，表现为 NPU 利用率突降、CPU 突涨；用 `npu-smi info -t utilization` 配合 `msprof` 定位。
- **内存池碎片**：长时间运行后 NPU 内存池碎片化，导致新请求 OOM 但 `npu-smi info` 显示空闲；重启进程或调用 `torch_npu.npu.empty_cache()` 缓解。
- **HCCL 通信超时**：跨 HCCS 域或 NUMA 远端时 allreduce 超时，HCCL 默认超时偏长，故障表现为训练/推理卡住而非报错。

### 关键环境变量

```bash
export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3
export TASK_QUEUE_ENABLE=1          # 异步 task queue，提升 launch 吞吐
export ASCEND_GLOBAL_LOG_LEVEL=2    # 0=debug 1=info 2=warning 3=error
export HCCL_CONNECT_TIMEOUT=7200    # 秒，HCCL 建链超时
export HCCL_EXEC_TIMEOUT=0          # 0=不超时，避免长 allreduce 误杀
```

### torch_npu ABI 要求

torch_npu 强制要求 `-D_GLIBCXX_USE_CXX11_ABI=0`（与 CUDA 早期生态类似）。第三方 C++ 扩展若用 `ABI=1` 编译，运行时会因 `std::string` ABI 不兼容而 symbol 找不到或段错误。

```bash
# CMake
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -D_GLIBCXX_USE_CXX11_ABI=0")
# pip install 自定义 op
CXXFLAGS="-D_GLIBCXX_USE_CXX11_ABI=0" pip install -e .
```

## vLLM KV Cache 大页

### PagedAttention 内存模型

vLLM 用 PagedAttention 把 KV cache 切成固定大小 block（通常 16 token/block），按需分配。KV cache 总量在推理启动时预分配到 HBM，因此 HBM 容量直接决定 max_num_seqs 和 max_model_len。

host 侧（CPU DDR）只承载权重加载、tokenizer、调度，理论上不直接吃 KV cache。但当 vLLM-Ascend 开启 CPU offload 或 swap 时，host 大页配置会显著影响 swap 性能。

### HugePages 配置

```bash
# 临时
echo 1024 > /proc/sys/vm/nr_hugepages       # 1024 * 2MB = 2GB
# 持久化 /etc/sysctl.conf
vm.nr_hugepages = 1024
sysctl -p

# 查看
grep -i huge /proc/meminfo
```

仅当确实存在 host 侧大块驻留（offload、swap、tokenizer cache）时再配大页；纯 HBM 推理配大页无收益。

### THP 配置

```bash
echo never   > /sys/kernel/mm/transparent_hugepage/enabled   # 推荐 NPU 推理
# 或
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled   # 仅对显式 madvise 区域
```

NPU 推理场景推荐 `never` 或 `madvise`：vLLM 的 KV cache 在 device 侧，host 侧大页反而增加 khugepaged 抖动与 TLB 抖动；`always` 下 khugepaged 合并页会引入不可预期的延迟尖刺。

### tcmalloc 拦截陷阱（实战关键发现）

`glibc` 大页调优依赖 `GLIBC_TUNABLES=glibc.malloc.tcache_count=...:glibc.malloc.hugetlb=1` 或 `mmap(MAP_HUGETLB)`。一旦 `LD_PRELOAD=libtcmalloc.so`，malloc 被拦截，glibc 大页路径不再被调用：

```
GLIBC_TUNABLES=glibc.malloc.hugetlb=1   # 失效
LD_PRELOAD=libtcmalloc.so                # 拦截 malloc → 走 tcmalloc 自己的页池
```

tcmalloc 有自己的 huge page 支持（`TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES`、`--tcmalloc_large_allocs`），与 glibc 大页不互通。**结论：tcmalloc 与 glibc 大页二选一，不能叠加。** 切换 allocator 时必须重新评估大页策略。

### 按模型大小决策

| 模型规模 | host 大页收益 | 说明 |
|----------|---------------|------|
| < 2B  | 无收益 | 内存压力小，TLB 不构成瓶颈，配大页徒增管理开销 |
| 2B–7B | 视 offload 而定 | 开启 CPU offload/swap 时可能有收益 |
| > 7B  | 可能有收益 | 长上下文 + offload 场景显著 |

实战：1.5B 模型在 Kunpeng 920 + Ascend 910 上配 2MB HugePages，吞吐与 P99 无变化，关闭后更稳。

## NPU-CPU 数据传输

### host-device 拷贝路径

| 方式 | API | 适用 |
|------|-----|------|
| 同步拷贝 | `aclrtMemcpy(..., ACL_MEMCPY_HOST_TO_DEVICE)` | 小张量、调试 |
| 异步拷贝 | `aclrtMemcpyAsync` + stream | 主流路径 |
| pinned memory | `aclrtMallocHost` 分配 host 锁页内存 | 提升异步拷贝吞吐 |
| 零拷贝 | NPU unified memory（`aclrtMallocManaged`） | 减少显式拷贝 |

锁页内存避免 host 侧 page fault 导致 DMA 中断，是高吞吐 H2D/D2H 的基础。PyTorch 侧 `tensor.pin_memory()` 在 torch_npu 下等价路径要确认 CANN 是否走 `aclrtMallocHost`。

### 零拷贝 / unified memory

CANN 提供 unified memory，NPU 与 CPU 共享虚拟地址、按需迁移。优势是省去显式拷贝；代价是缺页处理延迟不可控，对延迟敏感的在线推理慎用，离线 batch 推理可尝试。

### 内存池

频繁 `aclrtMalloc` / `aclrtFree` 会触发 CANN 内存池扩缩容与碎片。建议：

- vLLM-Ascend 启动时一次性预分配 KV cache 与权重 buffer，运行期不再释放。
- 自定义 op 避免在 forward 里 `new tensor`，用 pre-allocated buffer。
- 长跑服务定期 `torch_npu.npu.empty_cache()` 防碎片，但会引入短暂停顿，避开高峰。

### CPU feed 瓶颈

NPU 算得快、CPU 喂得慢是 A+K 推理典型瓶颈：

- tokenizer 多线程并行（HF tokenizers 支持 `use_fast=True` + `num_proc`）。
- DataLoader `num_workers>0`、`pin_memory=True`、`prefetch_factor>2`。
- 极致场景下把 tokenizer+DataLoader 绑定到 NPU 同 NUMA node 的 CPU 核，减少 feed 数据跨 NUMA。

## Ascend PMU 采集

### NPU 侧工具

```bash
# 利用率
npu-smi info -t utilization
# 板卡硬件信息（HBM 容量、温度、功耗）
npu-smi info -t board
# 详细实时
npu-smi info watch -t utilization -i 0

# Profiler（CANN 自带）
msnpureport -c on -d 0 -o ./prof_out
msprof --application="python run.py" --output=./prof_out \
       --aic-metrics=ArithmeticUtilization,L2CacheMiss \
       --aicpu=on --dvpp-profiling=off
```

`msprof` 输出包含 AI Core 算子耗时、L2Cache miss、HBM 带宽利用率、AICPU 占比，是 NPU 侧定位算子 fallback 与带宽瓶颈的核心工具。

### 与 CPU perf 的边界

- NPU 算子在 device 侧执行，CPU `perf record` 只能采到 launch（dispatch）与 wait（synchronize）的 host 调用，采不到算子内部。
- 算子耗时真相必须用 `msprof` / `npu-smi` 看，不要用 CPU perf 推断 NPU 算子热点。
- HCCL 通信的 host 侧栈可被 CPU perf 采到，能辅助定位通信阻塞是 host 调度还是 device 计算。

### ARM PMU 事件名差异

Kunpeng 920（ARMv8.2）与 x86 的事件名不同，常用对照：

| 含义 | Kunpeng / ARM PMU | x86 常见名 |
|------|-------------------|-----------|
| L1 指令缓存未命中 | `L1I_CACHE_REFILL` | `L1-icache-load-misses` |
| L3/LLC 未命中 | `LLC_CACHE_MISS` 或 `L3D_CACHE_REFILL` | `LLC-load-misses` |
| 分支预测失败 | `BR_MIS_PRED` | `branch-misses` |

perf 直接用 x86 事件名在 Kunpeng 上会报 "event not found"，必须查 `perf list` 或 `/sys/bus/event_source/devices/armv8_pmuv3_*/events`。

## A+K 联合优化

A+K = Ascend NPU（A）+ Kunpeng CPU（K），常见于国产化推理集群。联合优化除了各自平台调优外，还要处理编译链与运行时库的交叉依赖。

> **编译器与编译顺序依赖**：BiSheng 编译器选型、Python→PyTorch→torch_npu 编译顺序、PGO/LTO 编译参数属 `compiler-optimization` subskill 职责，详见 `subskills/compiler-optimization/references/ak-compiler-playbook.md`。本文件不重复编译器内容。

> **tcmalloc + libarcher 陷阱**：tcmalloc 部署的运行时依赖陷阱属 `performance-library-selection` subskill 职责，详见 `subskills/performance-library-selection/references/ascend-playbook.md` "tcmalloc 运行时依赖陷阱"章节。本文件不重复。

### NPU NUMA 亲和 × CPU 绑核

A+K 多卡推理推荐拓扑对齐：

1. `lspci -d 19e5:` 查每张 NPU 的 NUMA node。
2. 协调进程（vLLM worker、tokenizer）绑到同 NUMA 的 CPU 核。
3. 单卡场景优先用 NPU 同 NUMA 的 CPU 做 feed；多卡 TP 场景按 HCCS 域分组绑核。
4. IRQ 亲和：NPU 卡的 PCIe MSI 中断也按 NUMA 归属绑到同 node CPU，避免中断跨 node。

实战结论：在 Kunpeng 920 7285Z + Ascend 910 上，把 vLLM worker 与对应 NPU 卡同 NUMA 绑核，相比跨 NUMA，HBM 控制延迟和 H2D 拷贝延迟均有可观下降，长上下文场景吞吐提升明显。
