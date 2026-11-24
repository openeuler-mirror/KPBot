---
name: kptools-ascend-profiling-compare-free
description: |
  专项分析/对比：把 profiling 里的设备空闲（Free）拆成可行动的子类——
  host_busy（host 忙但没下发）、sync_wait（等同步）、dispatch_gap（下发完成但设备没启动）、
  comm_align（通信依赖对齐）、memcpy_gap（拷贝衔接空隙）、unknown。
  当用户问“Free 时间都花在哪了 / 空闲是 host 慢还是等待”时使用。
  只看 Free 总量与四分类走 kptools-ascend-profiling-compare；
  气泡窗口定位走 -compare-bubble；单份 trace 分析走 -analysis(-nostack)。
  注：compare / compare-bubble / compare-enqueue / compare-dequeue / compare-opstats 与本 skill 为
  同批 PR 交付，合入前暂用 kptools-ascend-profiling-analysis / -analysis-nostack 承接。
---

# Free 时间细化分类

## 适用范围

- 单份画像或两份对比：每步 Free 构成 + 全局汇总 + 最长空闲窗口定位。
- 与 `-compare-bubble` 的分工：本 skill 给**每步的构成与占比**（汇总口径），bubble 给**窗口级定位与分类**（事件口径）；两者结论不得互相矛盾。

## 输入

`trace_view.json`（必需）：`ProfilerStep#N` 为 step 边界、设备 kernel 为忙碌、host 事件用于归因。
缺失 step 时按可观测 host / 设备 / 同步 / memcpy 事件确定整窗（覆盖首个 kernel 前的 host 准备）。

## 工作流

```bash
python3 scripts/free_breakdown.py <base> [<cand>] --out-html report.html --json result.json \
  [--min-gap-ms 0.01] [--adjacent-ms 1.0] [--min-delta-ms 0.05]
```

## 归因规则（优先级高→低）

1. `host_busy`：窗口内 host 计算事件（`python_function` / `cpu_op` / `aten::`，排除 ProfilerStep）覆盖 ≥50%，**且**窗口内没有下发完成点。
2. `sync_wait`：窗口与 synchronize 类 host 事件有交集。
3. `dispatch_gap`：有 host 下发事件在窗口内结束。
4. `comm_align`：紧邻（±`--adjacent-ms`）的 kernel 是通信类（`hcom*` / Task Type 含 `hccl` / **cat=communication**；该类事件本身也计入设备忙碌区间）。
5. `memcpy_gap`：紧邻事件是拷贝类（memcpy/memset/transdata/h2d/d2h/d2d）。
6. `unknown`：以上都不满足。

**窗口级归因**：一个空闲窗口整体归给一个子类，不做部分切分；占比按窗口时长加权。

## 指标

| 指标 | 单位 | 含义 |
|---|---|---|
| `free_total_ms` | ms | Free 总量（仅统计 ≥ `--min-gap-ms` 的窗口；Δ>noise → improved） |
| `free_host_busy_ms` / `free_sync_wait_ms` / `free_dispatch_gap_ms` / `free_comm_align_ms` / `free_memcpy_gap_ms` / `free_unknown_ms` | ms | 六个子类（`info`，描述性） |
| `free_unknown_pct` | pct | unknown 占 Free 的比例（`info`） |

`unknown > 30%` 时写入 `degraded` 并在报告顶部告警。

## 输出

- 终端摘要、A.1 九字段 JSON、HTML（子类对比表、base 最长空闲窗口 Top10 带时间戳、降级与口径）。

## 降级路径（始终产出报告）

1. 无 `ProfilerStep#N` → 整窗归因，`confidence=low`，`per_step` 只有 1 条。
2. 无 host 事件 → 窗口多归 `unknown`，按 30% 阈值告警。
3. 无设备 kernel → 报错页 + 退出码 3（没有忙碌区间就无法定义 Free）。
4. 输入不可读 → HTML 错误页 + 退出码 3。

## 真实数据口径

- 设备 kernel：`args["Task Type"]` 属于 AI/拷贝任务（`cat=None`），或名字以 `hcom` 开头。
- `dispatch_gap`：真实 trace 有 `cat="enqueue"` 事件，其结束点落在空闲窗口内即判该窗口。
- `sync_wait`：`cpu_op` 里的 `Event::synchronize` 类事件。
- `host_busy`：`python_function` / `cpu_op` / `operator` 类事件，或名字含 `aten::`（排除 `ProfilerStep#` 与 synchronize）；与实现三处一致。
- `ProfilerStep#N` 在真实 trace 里 `cat=None`，只按名字识别；时间单位 µs。

## 限制

- 归因是启发式时间窗判断，不读取 runtime 队列；`unknown` 高说明证据不足，报告明确告警。
- 不输出气泡的窗口分类结论（bubble 职责）；两者口径差异写入 references。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_free -v
```
