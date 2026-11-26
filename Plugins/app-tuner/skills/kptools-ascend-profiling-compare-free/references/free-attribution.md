# Free 归因规则

## 1. 定义

- 设备忙碌 = 所有设备 kernel 区间的并集（`AI_*` / `MIX_*` / `hcom*`）。
- Free = step 区间 − 设备忙碌。
- 窗口级归因：一个 Free 窗口整体归入一个子类，不做部分切分。
- 归因时只统计时长 ≥ `--min-gap-ms`（默认 0.01ms）的空闲窗口，故 `free_total_ms` 略小于 `Free` 全量（短窗口不计入）。

## 2. 子类与优先级

| 优先级 | 子类 | 判据 | 行动含义 |
|---|---|---|---|
| 1 | `host_busy` | host 计算事件覆盖 ≥50% 且窗口内无下发完成 | host 算得太久/没及时下发 |
| 2 | `sync_wait` | 窗口与 synchronize 事件有交集 | host 在等设备/等结果 |
| 3 | `dispatch_gap` | 有 host 下发事件在窗口内结束 | 下发完成但设备没启动 |
| 4 | `comm_align` | 紧邻 kernel 是通信类 | 通信依赖对齐等待 |
| 5 | `memcpy_gap` | 紧邻事件是拷贝类 | 拷贝衔接空隙 |
| 6 | `unknown` | 都不满足 | 证据不足，不给结论 |

`host_busy` 要求「无下发完成」是为了避免把「刚下发完还没启动」误判成 host 忙。

## 3. 与 v1 Free、bubble 的关系

| 口径 | 回答 | 归属 |
|---|---|---|
| 每步 Free 总量/占比 | 空了多少 | `kptools-ascend-profiling-compare` |
| 每步 Free 的子类构成 | 空的原因（汇总） | **本 skill** |
| 具体窗口位置/时长/分类 | 空在哪 | `-compare-bubble` |

三者可以互相印证，但不得直接相加或互相替代；报告必须写明口径。

## 4. 告警

- `unknown_pct > 30%` → `degraded` + 报告顶部告警，说明归因证据不足。
- 无 ProfilerStep → `confidence=low`，按可观测 host/设备/同步/memcpy 事件确定整窗（不是只取首末 kernel 之间），首个 kernel 前的 host 准备也会参与归因。
- 无 host 事件 → unknown 会偏高，按上一条告警。

## 5. 噪声与单份模式

- `free_total_ms`：每步序列 MAD，`noise = max(--min-delta-ms, MAD(base)+MAD(cand))`。
- 子类时长为描述性指标（`info`），不做优劣判定。
- 单份模式（无 cand）：所有 metric 的 `cand`/`delta`/`delta_pct` 为 `null`、`verdict=info`；
  `per_step` 只给 `{step, base}`，不输出 `cand`/`delta`。
