# dequeue 口径定义

## 1. 与 enqueue 的关系

| 指标 | 定义 | 归属 |
|---|---|---|
| 下发耗时 | host API 事件 `dur` | `-compare-enqueue` |
| 排队等待（dequeue） | `设备 kernel start − host API end` | **本 skill** |
| 重叠/下发总量 | host API 区间与设备区间的关系 | `-compare-enqueue` |

两个 skill 共享事件识别与时间窗启发式配对，但侧重点不同：enqueue 也输出「下发→启动」等待分布（enqueue_to_start_p50/p95），本 skill 额外提供 correlation_id 精确配对（enqueue 无此路径）；本 skill 不输出 host API 自身耗时。二者输出字段需按实际指标名区分，不再宣称「一量一主」。

## 2. 配对算法

存在 `cat="dequeue"` 且带 `args["correlation_id"]` 时走精确配对，否则回退 kernel 启发式：

- **精确配对**：enqueue 与 dequeue 按 `correlation_id` 配对，`wait = max(0, dequeue.start − enqueue.end)`；缺 correlation_id 的 enqueue 不计入。
- **启发式（回退）**：
  1. host 事件按 start 升序。
  2. 每个 host 事件在 `[start, end + pair_window]` 内匹配首个未使用的 kernel。
  3. `wait = max(0, kernel.start − host.end)`；配对失败的 host 事件不计入等待，但计入配对率。

两种口径测的不是同一个量（队列等待 vs 设备启动延迟）；base/cand 口径不一致时等待指标不可直接比较，报告标 `degraded` 且 `confidence=low`。

## 3. 等待占比

```
share = Σwait / (Σwait + 设备 kernel 忙碌时长)
```

用途：判断排队是否是主要矛盾；占比高说明设备接单慢（下发不足 / 队列排队），
占比低说明 host 与设备并行良好。

## 4. 噪声与置信度

- 分位数噪声 = `MAD(base 等待) + MAD(cand 等待)`。
- 每步总量噪声 = `max(--min-delta-ms, MAD(base 每步) + MAD(cand 每步))`。
- 等待占比噪声 = 固定 `0.5` 个百分点下限（避免微小比例差异被判 improved/regressed）。
- 无 ProfilerStep、任一侧配对率 0，或两侧配对口径不一致（exact vs heuristic）→ `confidence=low`；配对率 < 50% 或配对样本 < 30 → 最高 `medium`。

## 5. 边界

- host API 自身耗时：`kptools-ascend-profiling-compare-enqueue`。
- Free 构成：`kptools-ascend-profiling-compare-free`。
- 流并发：`kptools-ascend-profiling-stream`。
