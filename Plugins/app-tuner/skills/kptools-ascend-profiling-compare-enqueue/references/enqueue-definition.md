# enqueue 口径定义

## 1. 术语

- **enqueue（下发）**：host 侧把算子提交到 runtime 的调用耗时，取 host API 事件的 `dur`。
- **enqueue→start**：host API 结束到对应设备 kernel 开始的间隔，反映“下发完多久设备才动”。
- **dequeue**（见 `-compare-dequeue`）：强调排队等待本身，与本 skill 的第二项指标同源，但报告焦点和口径字段不同。

## 2. 事件识别

| 类型 | 判据 |
|---|---|
| host 下发 | `cat ∈ {pytorch_api, cann_api, cann, operator}`，或 name 以 `AscendCL@` 开头、含 `aclnn` |
| 设备 kernel | `cat ∈ AI 任务类型`，或 name 以 `hcom` 开头 |
| step 边界 | `cat=cpu_op` 且 name 以 `ProfilerStep#` 开头 |

排除：`python_function`、`cpu_op` 中非 ProfilerStep 的算子（避免与 PYTORCH_API 重复计数）。

回退路径（无 `cat=enqueue`）额外排除阻塞/查询/资源管理调用：名称含
`synchronize` / `query` / `getdevice` / `setdevice` / `malloc` / `free` /
`eventrecord` / `eventwait` / `notifyrecord` / `notifywait` / `streamwait`。
回退会把来源写入 `degraded`，置信度降为 `medium`。

## 3. 配对算法

1. host 事件按 `start` 升序。
2. 对每个 host 事件，从**下发完成点**起的窗口 `[end, end + pair_window]` 内找**首个未使用**的 kernel（按 start 最小）；窗口下界是 host 结束而不是 host 起点，避免把下发期间已启动的 kernel 重新消费、把启动延迟算成 0。
3. 命中则记录 `latency = max(0, kernel.start − host.end)`。
4. 配对率 = 命中数 / host 事件数；低于 50% 时报告提示“结论置信度受限”。

## 4. 噪声与判定

- 分位数 / 下发→启动：`noise = max(--min-delta-ms, MAD(base 分布) + MAD(cand 分布))`。
- 每步总量：`noise = max(--min-delta-ms, MAD(base 每步序列)+MAD(cand 每步序列))`。
- 重叠率：`noise = --min-delta-pct`（默认 1.0 个百分点），越高越好（判定取反）。
- `Δ = base − cand`，正值表示候选更省。

## 5. 降级与置信度

| 情况 | 行为 | confidence |
|---|---|---|
| host + kernel + step 齐全且配对率 > 0 | 全指标 | high |
| 任一降级（含回退启发式下发识别） | 部分指标为 0 / 口径变粗 | medium |
| 无 ProfilerStep 或任一侧配对率 = 0 | 整窗 / 无法测量延迟 | low |

## 6. 步口径

- step 全集取 `ProfilerStep#N` 跨度；无下发的 step 以 0 计入每步总量分母。
- 落在所有 step 窗口外的 host 事件只进入分布类指标（p50/p95），不进入 per-step。
- 两侧步数不一致时只比较交集步，并写入 `notes`。

## 7. 边界

- 排队等待专项：`kptools-ascend-profiling-compare-dequeue`。
- Free 构成：`kptools-ascend-profiling-compare-free`。
- 通用对比：`kptools-ascend-profiling-compare` / `-compare-hub`。
