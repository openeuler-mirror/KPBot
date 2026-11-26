---
name: kptools-ascend-profiling-compare-steps
description: |
  单份采集内的 step / step 组对比：在同一份 Ascend/torch_npu profiling 里比较两组 step
  （或任意两步），用 MAD 噪声判定（逐对 Δ 中位数 + 相对下限 + 方向检查；可用 step <2 时拒绝组对比）给出步耗时、Computing、Communication(未重叠)、Free、
  Preparing 的组间差异，并定位最大劣化 step。
  只做**单份采集内**的 step 组对比；两份采集的整体对比走
  kptools-ascend-profiling-compare（v1）；需要切分 step 边界走
  kptools-ascend-profiling-step-split；设备气泡窗口走
  kptools-ascend-profiling-compare-bubble；不确定该用哪个对比能力走
  kptools-ascend-profiling-compare-hub。
---

# 同一份 profiling 内的 step 组对比

## 适用范围

- 一份采集里想让不同 step（或两组 step）互相比：定位坏 step、阶段性劣化、最好 vs 最坏。
- 支持显式指定 `--base-steps/--cand-steps`、`--base-step/--cand-step`；未指定时按 step 顺序前后对半切分。
- `mode` 固定为 `single`（只有一个采集）。

**不做**：

- 不比较两份采集（跨采集归 `kptools-ascend-profiling-compare`）。
- 不做 step 边界切分（无 step 标识时归 `kptools-ascend-profiling-step-split`）。
- 不做设备气泡窗口、算子统计等专项（归对应 skill）。

## 输入

一个采集目录：`*_ascend_pt` / `ASCEND_PROFILER_OUTPUT` / 裸 `trace_view.json`。
裸 trace 需同目录存在 StepTraceTime / STEP_TIME 侧车；无侧车时只能输出「无可用 step 数据」，切 step 请走 `kptools-ascend-profiling-step-split`。

每步指标读取优先级：

| 来源 | 字段 |
|---|---|
| `analysis.db:StepTraceTime` | `step, computing, communication_not_overlapped, communication, free, stage, preparing`（µs） |
| `step_trace_time.csv` | 同名列；兼容带空格/括号的列名 |
| `ascend_pytorch_profiler_*.db:STEP_TIME` | `startNs, endNs`：**步耗时优先取该跨度**（与 stage+preparing 等价，且不受 CSV 口径差异影响） |

注：本 skill 与 step-split / compare-bubble / compare-opstats / compare-hub / compare(v1) 等同批 PR 交付；合入前相关路由暂用 kptools-ascend-profiling-analysis / -analysis-nostack 承接。

## 工作流

```bash
# 两组 step
python3 scripts/step_groups.py <capture> --out-html r.html --json r.json \
  --base-steps 0,1,2 --cand-steps 5,6,7

# 任意两步
python3 scripts/step_groups.py <capture> --out-html r.html --json r.json \
  --base-step 3 --cand-step 7

# 跳过两组各自开头的 K 个 step
python3 scripts/step_groups.py <capture> --out-html r.html --json r.json \
  --base-steps 0,1,2,3 --cand-steps 4,5,6,7 --skip-warmup 1
```

算法：

1. 先在**共同配对样本**（前 `min(len(base), len(cand))` 对）上截齐，再取组内中位数作为代表值；
   `Δ = base组 − cand组`，正值表示候选组更快/更省。展示值与方向判定使用同一批配对样本，
   避免「只比首对、摘要取全量」自相矛盾。
   步耗时 `group_step_time_ms` 优先取 `STEP_TIME` 跨度；无 `STEP_TIME` 时用 `stage + preparing`，
   与侧车分量的四分类口径保持一致（`step_time = comp + comm_no + free + preparing`）。
2. 噪声带 `noise = max(--min-delta-ms, |base组中位数|×1%, MAD(逐对Δ))`；`|Δ| ≤ noise` → `verdict=noise`。
   配对中正负方向不一致（≥2 对且一致率 < 0.75）时判 `info`，不下优化/退化结论。
3. `per_step` 把两组 step 按位置配对（第 i 个 ↔ 第 i 个）；组大小不同时只比交集并写 `notes`。
4. `--skip-warmup K` 从两组各自开头去掉 K 个 step。

## 输出

- 终端摘要 + `--out-html` HTML；`--json` 仅在显式指定时写 A.1 信封（指标前缀 `group_`）。
- 固定五项指标：`group_step_time_ms`、`group_computing_ms`、`group_communication_ms`、`group_free_ms`、`group_preparing_ms`。
- 四分类不可用（仅 `STEP_TIME`）时，后四项 `verdict=info`（不是 `noise`）；部分列缺失时仅缺失指标为 `info`。
- HTML 必含：两组指标对比表、逐 step 配对表（标出最大劣化步）、噪声带说明、降级说明。
- `per_step` 字段：`step`（基线 id）、`cand_step`（候选 id）、`base`/`cand`/`delta`（步耗时 ms）。

## 输出契约（A.1）

```json
{
  "skill": "kptools-ascend-profiling-compare-steps",
  "schema_version": 1,
  "mode": "single",
  "inputs": {"base": "<capture>", "cand": "<capture>", "steps": "base=[0,1,2]; cand=[5,6,7]"},
  "data_quality": {"sidecars": [], "degraded": [], "confidence": "high | medium | low"},
  "metrics": [{"name": "group_step_time_ms", "unit": "ms", "base": 10.0, "cand": 12.0,
               "delta": -2.0, "delta_pct": -20.0, "verdict": "regressed",
               "noise_band": 0.05, "evidence": "ascend_pytorch_profiler_*.db:STEP_TIME[startNs,endNs]"}],
  "per_step": [{"step": 0, "cand_step": 5, "base": 10.0, "cand": 12.0, "delta": -2.0}],
  "summary": "...",
  "notes": ["..."]
}
```

- `verdict ∈ {improved, regressed, noise, info}`；每条 metric 带 `evidence`（侧车/表/字段）。
- 组大小 < 3 时 MAD 不可靠，`notes` 会标注，判定主要由 `--min-delta-ms` 决定。

## 降级路径

| 情况 | 行为 | 退出码 |
|---|---|---|
| 无 `StepTraceTime` / CSV，但有 `STEP_TIME` | 只有 `group_step_time_ms` 有效，其余四项为 0 且 `verdict=info`，`degraded` 写「四分类缺失」，`confidence=low` | 0 |
| 侧车缺 Preparing（或其它四分类列） | 仅缺失指标不可用（展示 0、`verdict=info`），其余指标照常判定；`degraded` 逐列记录，`confidence=low` | 0 |
| 侧车 step id 与 `STEP_TIME` 不一致 | 取并集：仅有 `STEP_TIME` 跨度的步保留并参与**步耗时**比较，缺侧车行时该步分量指标不可用 | 0 |
| 完全没有 step 数据 | HTML（指定 `--json` 时同时写 JSON），`metrics` 空，`summary="无可用 step 数据"`，不伪造数字 | 0 |
| 指定 step id 不存在 / 非整数 / 组选项为空 | 错误页（+ 指定 `--json` 时 JSON），错误信息带可用 step id 列表 | 2 |
| 两组大小不同 | 只比交集，`notes` 写明两侧步数 | 0 |
| 输入不可读 | HTML 错误页（指定 `--json` 时同时写 JSON） | 3 |

## 口径与限制

- `Δ = base − cand`，正值表示候选组更快/更省。
- 噪声判定：`noise = max(--min-delta-ms, |base组中位数|×1%, MAD(逐对Δ))`，逐对 Δ 只在共同 step 上计算；≥2 对且方向一致性 < 0.75 判 `info`。组代表值与方向判定都基于截齐后的共同配对样本。
- 组代表值用中位数；步耗时优先 `STEP_TIME` 跨度，缺省时用 `stage+preparing`（≈v1 口径），只做**同一份采集**内比较，不做跨采集时间戳对齐。
- 多 Device 行：Stage/Preparing 取 max，Computing/Communication/Free 均按 device 求和；Free 不再由 stage 反算，因此分量之和可能大于步耗时（步耗时取 max(stage)+max(prep) 或 STEP_TIME 跨度）。
- `step_trace_time.csv` 的 Stage/Free 可能与 `analysis.db`/`STEP_TIME` 不同；有 `STEP_TIME` 时步耗时以它为准，避免同一采集因侧车不同而结论不同。
- 默认 `--skip-warmup 0`：单份采集的前几个 step 可能是真实负载（如首个 prefill），自动跳步会静默改变用户选择；仅在确认存在 warmup 时显式指定。
- 未指定 step 组时按 step 顺序前后对半切分，仅作为兜底，报告会写明。

## 参考资料

- [references/step-group-methodology.md](references/step-group-methodology.md) — 组选择、按位置配对、warmup、噪声带、与 v1 两步对比的区别。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_steps -v
```

测试覆盖：两组中位数与判定、单步模式、组大小不一致取交集、skip-warmup、CSV 兜底、无四分类降级、无 step 数据、step id 不存在、不可读输入、SKILL/evals 完整性。
