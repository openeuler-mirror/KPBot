---
name: kptools-ascend-profiling-stream
description: |
  NPU stream 维度画像：每条 stream 的忙碌占比、流间并发度时间线与逐 step 并发度、
  Event Record→Wait 跨流依赖配对；支持单份画像，也支持两份采集的流拓扑变化对比。
  当用户明确要「stream 维度 / 每条流忙不忙 / 流间并发度 / 跨流依赖 / 流拓扑变化」时使用；
  只分析设备气泡（设备级空闲窗口）请用 `kptools-ascend-profiling-compare-bubble`，
  通用两份 profiling 对比（步耗时/四分类/瓶颈迁移）请用 `kptools-ascend-profiling-compare`，
  不确定该用哪个对比能力时走 `kptools-ascend-profiling-compare-hub`；
  单份 trace 的通用分析走 `kptools-ascend-profiling-analysis` / `-analysis-nostack`。
  只消费 trace_view.json，不做算子集合、拷贝、调用栈或融合分析。
---

# NPU stream 结构分析（stream）

注：compare / compare-bubble / compare-hub / compare-opstats / h2d-d2h 与本 skill 为同批 PR 交付，合入前暂用 kptools-ascend-profiling-analysis / -analysis-nostack 承接。

## 适用范围

- 单份采集：回答「用了哪些 stream、每条流多忙、流间并发度多高、有没有跨流依赖」。
- 两份采集：在各自画像之上给出流数量/拓扑变化、忙碌占比与并发度的 Δ 和噪声判定。
- 输入只需 `trace_view.json`（Ascend/torch_npu profiler 的 device 事件）。

不适用：

- 设备级「空闲窗口/气泡」定位 → `kptools-ascend-profiling-compare-bubble`。
- 步耗时、四分类占比、瓶颈迁移 → `kptools-ascend-profiling-compare`。
- 算子集合/shape/分位数、H2D/D2H 拷贝、调用栈、融合证据 → 对应专项 skill。
- 单份 trace 的通用结构分析（P1/P2/P3、Free 去向）→ `kptools-ascend-profiling-analysis` / `-analysis-nostack`。

## 输入

1. `*_ascend_pt` / `ASCEND_PROFILER_OUTPUT` 目录（自动找其中的 `trace_view.json`）。
2. 裸 `trace_view.json`。
3. 可选侧车 `ascend_pytorch_profiler_*.db:STEP_TIME`：仅用于在 trace 没有 `ProfilerStep#N` 时提供 step 边界。

stream 识别：设备事件的 `args["Physic Stream Id"]` / `streamId` / `stream_id` 优先，缺失时用 `tid`。
设备 kernel 过滤：`cat ∈ {AI_CORE, AI_VECTOR_CORE, MIX_AIC, MIX_AIV, AI_CPU, DSA_SQE}`
或算子名以 `hcom` 开头。**拷贝/DMA 不计入**：`Memcpy`/`Memset`/`TransData`、`SDMA_SQE`/`PCIE_DMA_SQE`
描述符和 PCIe DMA 任务都归 `kptools-ascend-profiling-compare-h2d-d2h`。

## 工作流

```bash
# 单份画像
python3 scripts/stream.py <capture> --out-html stream_report.html --json stream_result.json

# 两份对比（优化前后 / 两个配置 / 两次采集）
python3 scripts/stream.py <base> <cand> --out-html stream_compare.html --json stream_compare.json
```

1. 先确认输入来自同一负载（两份对比时，模型/版本/rank 是否一致由使用者确认）。
2. 读 HTML：并发度时间线、每流忙碌占比表、逐 step 平均并发度、跨流依赖清单、降级说明。
3. 需要机器聚合时读 `--json`（A.1 统一信封，指标前缀 `stream_`）。

## 输出

- HTML：并发度阶梯时间线（含 step 边界）、每流忙碌占比表、逐 step 并发度、跨流依赖清单、
  双份模式的流拓扑变化表、降级与口径说明。
- JSON：A.1 九字段信封。

| name | unit | 含义 |
|---|---|---|
| `stream_count` | count | 采集内出现的 stream 数 |
| `stream_busy_pct` | pct | 各流忙碌占比中位数（单流忙碌 = 该流 kernel 区间并集 / 采集窗口） |
| `stream_busy_max_pct` | pct | 最忙的一条流的忙碌占比 |
| `stream_concurrency_avg` | count | 全窗口时间加权平均并发 kernel 数（面积 / 窗口长度） |
| `stream_concurrency_max` | count | 峰值并发 kernel 数 |
| `stream_concurrency_per_step` | count | 每步平均并发度的中位数；逐 step 值在 `per_step` |
| `stream_cross_dep_count` | count | Event Record → Wait 配对成功且跨流的依赖数（配对率另见 `notes`，同流配对也计入配对率） |

单份输入：`mode=single`，`cand/delta/delta_pct` 为 `null`，只出画像。
两份输入：`mode=pair`，`Δ = base − cand`。stream 指标是描述性的，超出噪声带记
`verdict=info`，不做改善/恶化判定。

## 降级路径（始终产出 HTML + JSON）

1. 设备事件无 `tid` 且无 `args.streamId` → 全部归为单流，`degraded` 记「无法区分 stream」，`confidence=low`。
2. Event 事件存在但缺 event id 字段 → `stream_cross_dep_count=0`，`degraded` 记录，`confidence=medium`。
3. 无设备事件 → 空结果，`summary` 写「未发现设备 kernel」，`confidence=low`。
4. 无 `ProfilerStep#N` 且无 `STEP_TIME` → `per_step` 用整窗单步，`degraded` 记录，`confidence=low`。
5. 输入不可读 → HTML 错误页（指定 `--json` 时同时写 JSON），退出码 3。

## 口径与限制

- trace 的 `ts`/`dur` 按 µs 解释并统一换算为 ns；报告时间以 ms 展示。
- 采集窗口：有 `ProfilerStep#N` 时取「首步起点→末步终点」；否则回退 `[首个 kernel 起点, 末个 kernel 终点]`。忙碌是**单条流**的区间并集。
- 并发度用 kernel 端点扫描线：同刻先处理终点再处理起点，因此首尾相接的 kernel 不算并发。
- 设备级空闲窗口归 `kptools-ascend-profiling-compare-bubble`，**两者基准不同，结论不可互推**。
- 跨流依赖只做事件级配对（event id），不还原 engine/stream 的完整时序语义；配对率 <50% 时报告显式提示“依赖数与拓扑结论需打折”。
- 本 skill 只消费 trace，按 A.1 置信度规则最高为 `medium`（trace-only）；出现整窗/单流归并降为 `low`。
- `--json` 仅在显式指定时写文件；默认只写 `--out-html`。
- `--min-delta-ms` 为 CLI 兼容参数；stream 指标单位为 pct/count，噪声带只用 MAD，不套用毫秒下限。

## 参考资料

- [references/stream-model.md](references/stream-model.md) — stream 识别、忙碌/并发度定义、依赖配对口径、与 bubble 的边界。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_stream -v
```

## 真实数据口径

- **设备 kernel**：真实 CANN trace 的 `cat=None`，任务类型在 `args["Task Type"]`（`AI_CORE` / `AI_VECTOR_CORE` / `MIX_AIC` 等）；`Memcpy`/`SDMA_SQE`/`PCIE_DMA_SQE` 等拷贝/DMA 任务不计入（归 h2d-d2h）。本 skill 同时兼容 `cat∈AI_*` 的旧格式。
- **stream 标识**：优先 `args["Physic Stream Id"]`（真实字段），其次 `streamId`/`stream`，最后 `tid`。
- **跨流依赖**：真实数据用 `Notify_Record` / `Notify_Wait` + `notify_id`（兼容 `Event Record/Wait` + `Event ID`）。
- **忙碌占比分母**：有 `ProfilerStep#N` 时用「采集步窗口」（首步起点→末步终点），避免预热/收尾空闲稀释。
- **时间单位**：trace 的 `ts/dur` 一律按 µs 解释（不做 ns 猜测）。
