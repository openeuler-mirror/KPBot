---
name: kptools-ascend-profiling-compare-enqueue
description: |
  专项对比：两份 Ascend/torch_npu profiling 的 host 下发（enqueue）时延——
  每算子下发耗时分布（p50/p95）、下发→设备启动延迟、每步下发总量、下发与设备执行的重叠比。
  当用户明确问“host 下发是不是变慢了 / 下发→启动延迟多大 / 是不是 host 阻塞”时使用。
  通用“优化前后差多少”走 kptools-ascend-profiling-compare-hub 或 -compare；
  排队等待（下发完成→设备启动）的专项对比用 -compare-dequeue；
  Free 构成看 -compare-free；单份 trace 分析走 kptools-ascend-profiling-analysis(-nostack)。
---

# Host 下发（enqueue）时延对比

## 适用范围

- 两份采集的 host 下发链路对比；本 skill 不做单份画像（单份下发专项走 `kptools-ascend-profiling-host-dispatch`，规划中）。
- 不下结论“设备为什么空闲”（free/bubble 的职责）。

## 输入

`trace_view.json`（必需）。事件识别：

- host 下发：`cat ∈ {PYTORCH_API, CANN_API, cann, operator}`，或名字以 `AscendCL@` 开头、含 `aclnn`。
- 设备 kernel：`cat ∈ {AI_CORE, AI_VECTOR_CORE, MIX_AIC, MIX_AIV, AI_CPU, DSA_SQE}` 或名字以 `hcom` 开头。
- step 边界：`ProfilerStep#N`（`cat=cpu_op`）；缺失时整窗统计并降级。

## 工作流

```bash
python3 scripts/enqueue.py <base> <cand> --out-html report.html --json result.json \
  [--pair-window-ms 5.0] [--min-delta-ms 0.05] [--min-delta-pct 1.0]
```

## 指标与口径

| 指标 | 单位 | 含义 | 方向 |
|---|---|---|---|
| `enqueue_p50_ms` / `enqueue_p95_ms` | ms | host 下发调用耗时分布 | 越少越好 |
| `enqueue_total_per_step_ms` | ms | 每步下发总耗时 | 越少越好 |
| `enqueue_to_start_p50_ms` / `enqueue_to_start_p95_ms` | ms | 下发完成 → 设备 kernel 启动 | 越少越好 |
| `enqueue_pair_rate_pct` | pct | 成功配对比例（配对是启发式） | `info` |
| `dispatch_overlap_pct` | pct | host 下发区间 ∩ 设备 kernel 区间 ÷ 下发忙碌 | 越高越好（判定取反） |

- 配对规则：按 host 结束时刻排序，从下发完成点起 `[end, end + --pair-window-ms]`（默认 5ms）内找**首个未匹配**的设备 kernel；配对率必须写进报告。
- `Δ = base − cand`；噪声带：所有时间类指标（p50/p95、下发→启动、每步总量）都不小于 `--min-delta-ms`，重叠率另用 `--min-delta-pct`（默认 1.0 个百分点）作下限。
- 每步下发以 `ProfilerStep#N` 的**真实编号**为 step 全集，无下发的 step 计 0；窗口外 host 事件只进分布、不进 per-step。
- `confidence`：有 step 且有配对且无降级 = `high`；任一侧降级（含回退到启发式下发识别）= `medium`；无 step 或任一侧配对率 0 = `low`。

## 输出

- 终端摘要、A.1 九字段 JSON、HTML（指标表、候选侧下发最慢 API、降级与口径）。

## 降级路径（始终产出报告）

1. 未识别到 host API → 指标为 0 并记 `degraded`。
2. 无 `cat=enqueue`、回退 API 名/类别识别 → 排除 synchronize/query/malloc/event/notify 等非下发调用，并记 `degraded`（置信度 medium）。
3. 未识别到设备 kernel → 配对率 0、置信度 `low`，仍出报告。
4. 无 `ProfilerStep#N` → 整窗统计，`per_step` 只有 1 条。
5. 输入不可读 → HTML 错误页 + 退出码 3。

## 真实数据口径

- **下发事件**：真实 trace 直接提供 `cat="enqueue"`（`Enqueue@<api>`），本 skill 优先只用这些事件；无则回退 PYTORCH_API/CANN_API/aclnn/`AscendCL@` 启发式，并排除 synchronize/query/malloc/event/notify 等阻塞或管理类调用。
- **设备任务**：`args["Task Type"]`（`cat=None`），配对时按 start 排序 + 单调指针，避免 O(H×K)。
- **配对**：下发完成 → 窗口内首个未匹配设备任务；配对率写入指标，低于 50% 时结论要打折。
- 时间单位 µs；`ProfilerStep#N` 只按名字识别。

## 限制

- 配对是**启发式**（时间窗 + 顺序消费），不是 runtime 的真实队列记录；配对率 < 50% 时报告显式提示、结论要打折。
- 不区分不同 stream 的排队细节（那是 dequeue/stream 的职责）。
- `correlation_id` 精确配对未在本版实现（本 skill 只读 trace 时间窗）；如需精确配对见 kptools-ascend-profiling-compare-dequeue。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_enqueue -v
```
