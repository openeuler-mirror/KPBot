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
