# stream 模型与口径

本文件定义 `kptools-ascend-profiling-stream` 的识别与计算口径。所有结论只回答
「stream 维度」，不替代设备气泡或通用对比结论。

## 1. stream 识别

输入为 `trace_view.json` 的 X 事件。按以下顺序确定一条设备 kernel 所属的 stream：

1. `args` 中的 stream 字段按**固定优先级**取值：`Physic Stream Id`（物理流）→ `streamId/stream_id`（逻辑流）→ `logicalStreamId`，与 JSON 字段顺序无关；
2. 事件 `tid`（CANN device 事件的 tid 即 stream id）。

只有当**所有**设备 kernel 都取不到上述标识时，才视为「无法区分 stream」：全部归为
单流（stream key = `all`），写一条降级并降置信度。

设备 kernel 过滤：

- `cat ∈ {AI_CORE, AI_VECTOR_CORE, MIX_AIC, MIX_AIV, AI_CPU, DSA_SQE}`；
- 或算子名（小写）以 `hcom` 开头（通信 kernel）。

`Memcpy`/`Memset`/`TransData` 以及 `SDMA_SQE`/`PCIE_DMA_SQE` 描述符、PCIe DMA 任务不计入本 skill 的 kernel 集合（拷贝归 h2d-d2h 专项）。

## 2. 采集窗口

```
window = 有 ProfilerStep#N 时取「首步起点→末步终点」的采集步窗口；否则 [min(kernel.start), max(kernel.end)]
```

窗口由输入数据自身决定：有 `ProfilerStep#N` 时取采集步窗口（首步起点→末步终点），
否则才回退到 kernel 极值 `[min(kernel.start), max(kernel.end)]`；不使用 trace 全量事件边界。
因此「忙碌占比」回答的是「在该窗口内这条流有多少时间在跑」，不是整份采集的比例。

## 3. 单流忙碌占比

对每条流的全部 kernel 区间做并集（相邻/重叠区间合并），求和后除以窗口长度：

```
busy_pct(stream) = union(kernels[stream]) ∩ window / window_len × 100
```

`stream_busy_pct` 取所有流的中位数，`stream_busy_max_pct` 取最大值。

## 4. 并发度（时间加权）

对所有 kernel 的端点做扫描线：

- 起点 `+1`，终点 `−1`；
- 同一时刻先处理终点（半开区间 `[start, end)`），首尾相接的 kernel 不计为并发；
- 相邻同并发数区间合并后得到 `[(t0, t1, concurrency)]` 阶梯曲线。

```
avg = Σ concurrency × (t1 − t0) / window_len
max = max(concurrency)
```

逐 step 并发度：对每个 step 区间 `[s, e]`，用同一条曲线在区间内积分后除以 `e − s`。
`stream_concurrency_per_step` 取这些逐 step 值的**中位数**；逐 step 明细写入 `per_step`。

## 5. step 边界

优先级：

1. trace 内 `ProfilerStep#N` 事件（按 ts 排序，**保留真实 N 编号**，不重编号）；
2. `ascend_pytorch_profiler_*.db` 的 `STEP_TIME(id,startNs,endNs)`；
3. 都没有 → 用整窗作为单步，写降级，置信度降为 `low`。

## 6. 跨流依赖配对

1. 找 `name` 或 `cat` 含 `Event` 且含 `Record`/`Wait` 的事件；
2. 从 `args` 取 event id，优先级 `notify_id` → `event id`/`eventId`/`event_id`/`eventHandle` → `id`（键名归一化后匹配，避免通用 `id` 抢先）；
3. 每个有 id 的 Wait，按 event id 找时间上不晚于它的最近 Record；找不到则取同 id 的任意 Record；
4. Record 与 Wait 所属 stream 不同才计入 `stream_cross_dep_count`（同流配对数不计）；
5. 配对率 = 成功配对 Wait 数 / 有 id 的 Wait 数（**同流配对也计入分子**），跨流依赖数单列，
   写入 `notes`；<50% 时追加“结论需打折”提示。
6. Record 按 event id 预建时间索引；每个 Wait 用二分查找该 id 下不晚于 wait 时间的最近 Record。

没有 id 字段 → 依赖数记 0 并写降级；完全没有 Event 事件 → 依赖数记 0，只写说明、不降级
（可能确实无依赖，也可能该 tracing 版本不记录事件）。

## 7. 双份对比

- 两次各自算上述指标，`Δ = base − cand`。
- 噪声带：忙碌类用两次「每流忙碌占比」分布的 `MAD(base)+MAD(cand)`；并发度类用两次
  「逐 step 平均并发度」分布的 MAD 之和；计数类（流数、依赖数）无分布，噪声带为 0。
- `|Δ| ≤ noise` → `verdict=noise`；否则 `verdict=info`。stream 指标是描述性的，
  不用 improved/regressed 表示「更好/更差」。
- 拓扑表按 stream 对齐：只在 base 出现 = 消失，只在 cand 出现 = 新增，否则共有。
- `per_step` 按真实 step id 对齐（两侧 step 号不同也不会错配）；缺对应样本的步骤保留并给 `null`。

## 8. 与 bubble 的边界

| 维度 | 本 skill | `kptools-ascend-profiling-compare-bubble` |
|---|---|---|
| 基准 | 单条 stream 自己的 kernel 区间并集 | 所有流合并后的设备区间 |
| 回答 | 某条流忙不忙、流间是否并行、跨流依赖 | 设备整体在哪个窗口空闲、属于哪类 |
| 结论 | 不能推算设备空闲占比 | 不能推算单流忙碌占比 |

两者结论**不可互相换算**，报告不得混用。
