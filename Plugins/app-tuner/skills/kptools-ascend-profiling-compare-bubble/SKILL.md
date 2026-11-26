---
name: kptools-ascend-profiling-compare-bubble
description: |
  设备气泡窗口专项：在 Ascend/torch_npu profiling 的设备时间轴上定位「应执行而空闲」的窗口，
  给出位置（绝对起止时间戳）、时长与三类归因（不可归因空窗 / 通信等待 / 同步），并支持两份采集前后对比。
  只做设备气泡窗口专项：**Free 占比结论看 kptools-ascend-profiling-compare（v1）**；
  stream 并发结构看 kptools-ascend-profiling-stream；通用“两份对比/瓶颈变化/优化前后”看
  kptools-ascend-profiling-compare-hub；单份 trace 整体分析看
  kptools-ascend-profiling-analysis / -analysis-nostack。
---

# 设备气泡窗口检测与对比

## 适用范围

- 用户明确要看“设备空窗 / idle gap / 气泡 / 气泡在哪、属于什么原因、前后有没有减少”。
- 输入可以是一份或两份采集（`*_ascend_pt` 目录 / `ASCEND_PROFILER_OUTPUT` / 裸 `trace_view.json`）。
- 单份输入只输出窗口清单与分类；两份输入额外给出数量、总时长、最长、分类时长的 Δ 与噪声判定。

**不做**：

- 不输出“气泡占比 / Free 占比”结论——Free 口径归 `kptools-ascend-profiling-compare`（v1）。
- 不做 stream 级空闲、跨流依赖——归 `kptools-ascend-profiling-stream`。
- 不做第四类「下发不足」——依赖 host 下发数据（T1-2），本批标 `unsupported`。

## 输入

| 输入 | 用途 |
|---|---|
| `trace_view.json` | 设备忙碌区间（`cat`/`args["Task Type"] ∈ `AI_TASK_TYPES`（含 `AI_CORE`/`MIX_AIC`/`AI_CPU` 等）或拷贝引擎 `SDMA_SQE`/`PCIE_DMA_SQE`/`WRITE_VALUE_SQE`/`MEM_WRITE_VALUE`、`cat == COMMUNICATION` 或 `hcom*`）与 host synchronize 事件；无设备任务事件时回退 Overlap Analysis（`Computing` / `Communication`） |
| `analysis.db:StepTraceTime` / `step_trace_time.csv` | 无 device 事件时的忙碌窗口近似；标注采集为 native |
| `ascend_pytorch_profiler_*.db:STEP_TIME` | 每步绝对边界，用于 `per_step` 归属 |
| `profiler_info_*.json` | torch_npu / CANN 版本、rank |

有 `ProfilerStep#N` 时也可从 trace 取 step 边界（无侧车降级）。

**多 rank 输入**：目录下检测到多个 rank/capture 时，默认按 rank 号**确定性取最小**（同 rank/未知 rank 时按路径排序，不依赖文件系统顺序），并在 `notes`/`degradations` 写明“检测到 N 个 rank，仅分析 rank=X”，confidence 降为 `medium`；可用 `--rank N` 指定。`base`/`cand` 元数据带 `selected_rank`/`available_ranks`。

## 工作流

```bash
# 双份对比
python3 scripts/bubble.py <base> <cand> --out-html report.html --json bubbles.json

# 单份分析
python3 scripts/bubble.py <capture> --out-html report.html --json bubbles.json

# 多 rank 采集：指定 rank（缺省取最小 rank）
python3 scripts/bubble.py <capture> --rank 0 --out-html report.html --json bubbles.json

# 调窗口阈值（示例：显式调大到 1ms；默认 0.01ms）
python3 scripts/bubble.py <base> <cand> \
  --min-gap-ms 1.0 --adjacent-ms 1.0 --max-bubble-ms 200 \
  --min-delta-ms 0.05 --min-delta-count 1 \
  --out-html report.html --json bubbles.json
```

算法：

1. 设备忙碌区间 = 设备任务事件（`AI_TASK_TYPES`、拷贝引擎 `SDMA_SQE`/`PCIE_DMA_SQE`/`WRITE_VALUE_SQE`/`MEM_WRITE_VALUE`、`COMMUNICATION`、`hcom*`）区间的并集；`NOTIFY_WAIT`/`EVENT_WAIT` 等等待类不视为忙碌。
2. 窗口范围 = `[第一个 kernel 开始, 最后一个 kernel 结束]`，前后静默区不算气泡。
3. 空闲窗口 = 窗口 − 忙碌并集；短于 `--min-gap-ms`（默认 0.01）忽略；可用 `--max-bubble-ms` 过滤异常长尾（默认不过滤）。
4. 分类优先级：`sync`（窗口内有 host synchronize 事件）→ `comm_wait`（±`--adjacent-ms` 内相邻 kernel 为 `hcom*` / `COMMUNICATION`）→ `compute`（不可归因空窗）。
   备注：真实 trace 中设备任务类型常在 `args["Task Type"]` 而非 `cat`，两者都会读取。
5. 两份采集各跑一遍；`per_step` 按窗口起点落在哪个 step 归属。
6. 规模：busy/sync/comm/steps 预排序 + bisect，整体 `O((B+N)·logN)`；10 万事件/10 万气泡实测亚秒级。相邻 kernel 名只对 HTML Top-10 计算。
7. 大 trace（>512MB）：逐对象 `raw_decode` 兜底，Task Type 只取当前事件 args，不跨事件串染；解析事件数明显少于 `"ph":"X"` 标记数时写 `degradations`。

## 输出

- 终端摘要 + `--json`（A.1 信封，指标前缀 `bubble_`）+ `--out-html` HTML。
- 固定六项指标：`bubble_count`、`bubble_total_ms`、`bubble_max_ms`、`bubble_compute_ms`（不可归因空窗）、`bubble_comm_wait_ms`、`bubble_sync_ms`。
- `compute` 类表示「不可归因空窗」：窗口内无设备任务且不邻接通信/同步，只说明无法归因，不证明是计算气泡或存在因果。
- HTML 必含：气泡时间轴（窗口色块）、Top 10 最长窗口（带绝对起止 ns 时间戳）、分类时长表、降级说明。
- `per_step`：每步的 `bubble_total_ms`；无 step 边界时为空。

## 输出契约（A.1）

```json
{
  "skill": "kptools-ascend-profiling-compare-bubble",
  "schema_version": 1,
  "mode": "pair | single",
  "inputs": {"base": "...", "cand": "...", "steps": "base:...; cand:..."},
  "data_quality": {"sidecars": [], "degraded": [], "confidence": "high | medium | low"},
  "metrics": [{"name": "bubble_total_ms", "unit": "ms", "base": 2.0, "cand": 1.0,
               "delta": 1.0, "delta_pct": 50.0, "verdict": "improved",
               "noise_band": 0.05, "paired_delta": 1.0, "sign_consistency": 1.0,
               "evidence": "busy_source: ..."}],
  "per_step": [{"step": 0, "cand_step": 0, "base": 2.0, "cand": 0.5, "delta": 1.5}],
  "summary": "每步气泡总时长中位数 2.000ms → 1.000ms（Δ +1.000ms，改善）；整窗总时长 ...",
  "notes": ["..."]
}
```

- pair 模式下 `base/cand` 是 **per-step 中位数**；无 step 边界时 `bubble_count` 取气泡总数、`bubble_max_ms` 取最长气泡、`bubble_total_ms` 取总时长（序列只有 1 个元素），其余按逐气泡序列取中位数。`Δ = base − cand`，正值表示候选更省。
- 有 step 时按位置配对，`paired_delta = median(逐对 Δ)`；时长类噪声带 `max(--min-delta-ms, MAD(逐对 Δ))`，计数类噪声带 `max(--min-delta-count, MAD(逐对 Δ))`（默认 1 个气泡），`verdict` 由 `paired_delta` 判定；无 step 时用上述同一量纲的差值（count 为总数差、max 为最长气泡差、total 为总时长差）与两侧离散度。**verdict 不随 step 数变化**。
- summary 里的“整窗总时长/数量”是聚合值，与 metrics 的 per-step 中位数不是同一层级。
- `verdict ∈ {improved, regressed, noise, info}`；`mode=single` 时 `base` 保留整窗聚合、`cand/delta/delta_pct` 为 `null`、`verdict=info`。
- 每条 metric 的 `evidence` 写明 busy 来源、事件口径与阈值。
- `base`/`cand` 只含元数据（`label/path/kind/busy_source/step_source/window/sidecars/notes/degradations/version/cann_version/rank/selected_rank/available_ranks`），**不含** `busy`、`steps`、`busy_items`、`sync_events` 等原始区间。
- JSON **不落**原始 busy 区间与逐气泡明细（避免随事件数线性膨胀）；逐窗口 Top 10 明细在 HTML 中。HTML 时间轴超过 2000 段时按 800 色带聚合显示，per_step 表超过 200 步时只展示前 200 步并注明其余见 JSON。

## 降级路径

| 情况 | 行为 | confidence |
|---|---|---|
| 无侧车，只有 `trace_view.json` | 纯 trace 设备事件 | medium |
| 无 device 事件（只有 host），也无侧车 | 空气泡列表 + `notes` 说明，不报错 | low |
| 无 device 事件但 `StepTraceTime` 可用 | 按每步 `Computing+Communication` 近似忙碌窗口，标注近似 | medium |
| 无 step 信息 | 只出窗口级结果，`per_step` 为空 | low |
| 单份输入 | `mode=single`，只出窗口清单与分类，不做 Δ | 按 step 边界判定 |
| 输入不可读 | HTML 错误页 + JSON（空 metrics + 原因；错误壳非 A.1，须以退出码 3 判定失败） | low |

## 窗口阈值说明

`--min-gap-ms` 默认 0.01ms（10µs）：Ascend 单 step 内 kernel 之间的真实空闲常只有几十 µs，vLLM 的 dispatch 间隙也大量短于 1ms，用 1ms 会漏掉约 65% 的气泡时长；默认取 10µs 以保留这些短间隙。按负载可调大以滤掉调度抖动，报告会记录实际取值。step 之间的空洞是否算气泡取决于是否落在窗口范围内。

## 参考资料

- [references/bubble-definition.md](references/bubble-definition.md) — 忙碌/空闲/气泡定义、三类判据、「下发不足」为何 unsupported、与 v1 Free 的分工。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_bubble -v
```

测试覆盖：两个已知气泡窗口的召回与时长、三类分类各 1 例、无 device 事件降级、单份模式、不可读输入错误页、双份 A.1 信封、step 数不一致提示、A.1 校验（空 metrics / 字段类型不合法被拒绝）。

## 真实数据口径

- 设备任务：真实 trace 的 `cat=None`，类型在 `args["Task Type"]`（AI 任务与 `SDMA_SQE`/`PCIE_DMA_SQE` 拷贝任务都占设备）。
- `ProfilerStep#N` 在真实 trace 里 `cat=None`，只按名字识别。
- 默认 `--min-gap-ms` 按实测改为 **0.01**（10µs）：vLLM 的 dispatch 间隙大量短于 1ms，用 1ms 会丢掉约 65% 气泡时长。
- 与 `-compare-free` 交叉核对（vLLM Qwen3-30B-A3B）：气泡 21.42ms vs 空闲 22.37ms，sync 10.02 vs 10.12ms，口径一致。
