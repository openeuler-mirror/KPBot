---
name: kptools-ascend-profiling-compare-dequeue
description: |
  专项对比：两份 Ascend/torch_npu profiling 的 host dequeue（排队等待）时延——
  下发完成到设备 kernel 启动的等待分布（p50/p95）、每步等待总量、等待在运行时间中的占比。
  当用户问“下发完为什么设备还没动 / 排队等待多久 / 等待是不是主要矛盾”时使用。
  host 下发调用自身耗时看 -compare-enqueue；Free 构成看 -compare-free；
  通用对比走 -compare-hub 或 -compare；单份 trace 分析走 -analysis(-nostack)。
---

# Host Dequeue（排队等待）时延对比

## 适用范围

- 两份采集的「下发完成 → 设备启动」等待对比。
- 与 `-compare-enqueue` 同源但焦点不同：enqueue 看 host API 自身耗时，dequeue 看提交后的排队等待。

## 输入

`trace_view.json`（必需）。事件识别与配对规则与 enqueue 一致：

- host 下发：`cat ∈ {PYTORCH_API, CANN_API, cann, operator}` 或名字以 `AscendCL@` 开头、含 `aclnn`
- 设备 kernel：AI 任务类型或 `hcom`
- step 边界：`ProfilerStep#N`

## 工作流

```bash
python3 scripts/dequeue.py <base> <cand> --out-html report.html --json result.json \
  [--pair-window-ms 5.0] [--min-delta-ms 0.05]
```

## 指标与口径

| 指标 | 单位 | 含义 | 方向 |
|---|---|---|---|
| `dequeue_p50_ms` / `dequeue_p95_ms` | ms | 等待分布（exact: dequeue.start − enqueue.end；heuristic: kernel.start − host.end） | 越少越好 |
| `dequeue_total_per_step_ms` | ms | 每步等待总量 | 越少越好 |
| `dequeue_queue_wait_share_pct` | pct | 总等待 ÷（总等待 + 设备忙碌） | 越低越好 |
| `dequeue_pair_rate_pct` | pct | 配对成功率（exact=correlation_id 配对 / heuristic=时间窗配对） | `info` |

- `Δ = base − cand`；噪声带：分位数用 MAD 分布，总量用每步序列 MAD 且不小于 `--min-delta-ms`，占比用固定 0.5 个百分点下限。
- 等待占比用于判断「排队」是否是主要矛盾：占比显著下降说明提交后设备更快接单。
- `confidence`：配对率 ≥ 50% 且配对样本 ≥ 30、无降级 = `high`；配对率 < 50% 或样本 < 30 或存在降级 = `medium`；无 step、配对率 0 或两侧配对口径不一致 = `low`。

## 输出

- 终端摘要、A.1 九字段 JSON、HTML（指标表、候选侧等待最长的下发、降级与口径）。

## 降级路径（始终产出报告）

1. 无 host API → 指标 0 + `degraded`。
2. 无设备 kernel：exact 模式（有 correlation_id）仍可配对，配对率可 100%、`confidence=medium`；heuristic 模式无 kernel → 配对率 0、`confidence=low`。
3. 无 `ProfilerStep#N` → 整窗统计。
4. 输入不可读 → HTML 错误页 + 退出码 3。

## 真实数据口径

- **精确配对**：真实 trace 的 `cat="enqueue"` 与 `cat="dequeue"` 共享 `args["correlation_id"]`，本 skill 优先用它配对，`wait = dequeue.start − enqueue.end`。
- 无 correlation_id（或无法精确配对）时回退到「下发完成 → 设备任务启动」的启发式配对（配对率同样写进指标）。
- **口径可比性**：exact 与 heuristic 测的是不同量（队列等待 vs 设备启动延迟）；两侧配对口径不一致（一个 exact、一个 heuristic）时标 `degraded` 且 `confidence=low`，等待指标不可直接比较。
- 真实数据上 enqueue/dequeue 可达数千条，配对用排序 + 桶，避免 O(H×K)。
- 时间单位 µs；`ProfilerStep#N` 只按名字识别。

## 限制

- heuristic 回退与 enqueue 同一套启发式配对（时间窗 + 顺序消费），不是 runtime 队列记录；配对率低或样本少时结论打折。
- 不区分多 stream 的排队先后；流维度细节见 `kptools-ascend-profiling-stream`。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_dequeue -v
```
