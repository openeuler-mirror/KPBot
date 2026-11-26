# step 组对比方法学

## 1. 适用边界

- 本 skill 只处理**一份采集内**的 step / step 组对比。
- 两份采集（优化前后、两个版本/配置）必须走 `kptools-ascend-profiling-compare`（v1），
  因为它负责跨采集的 step 位置对齐、版本一致性告警与瓶颈迁移结论。
- 没有 step 边界时，先用 `kptools-ascend-profiling-step-split` 切 step，再回到本 skill。

## 2. 组选择

| 方式 | CLI | 说明 |
|---|---|---|
| 两组 | `--base-steps 0,1,2 --cand-steps 5,6,7` | 组内取中位数 |
| 单步 | `--base-step 3 --cand-step 7` | 各自组大小为 1 |
| 未指定 | 无 | 按 step 顺序前后对半切分（兜底，报告写明） |

- 组选项与单步选项不能混用。
- 指定的 step id 必须存在且为整数；不存在/非整数/组为空时退出码 2，并打印可用 id 列表。
- `--skip-warmup K` 在**选定组之后**从两组各自开头去掉 K 个 step；若因此出现空组，
  视为选择错误（退出码 2）。

## 3. 配对规则

- 两组 step **按位置配对**：第 i 个基线 step ↔ 第 i 个候选 step，使用各自实际的 step id 记录。
- 不按 step id 数值配对：两组通常语义是“不同迭代/不同阶段”，id 相同只是巧合。
- 组大小不同时只比较前 `min(len(base), len(cand))` 对，`notes` 写明两侧步数。
- `per_step.delta` 为步耗时差：`base − cand`，负值表示候选 step 更慢。

## 4. 组代表值与噪声带

```
n        = min(len(base组), len(cand组))
base     = median(base组前 n 步指标)      # 只在共同配对样本上截齐
cand     = median(cand组前 n 步指标)
Δ        = base − cand                     # 正 = 候选更快/更省
noise    = max(min_delta_ms, |base|×1%, MAD(逐对Δ))
Δ >  noise  -> improved
Δ < -noise  -> regressed
否则        -> noise
```

- 展示值（base/cand/Δ）和方向判定使用同一批共同配对样本，避免「逐对只比首对、摘要取全量」不一致。
- `MAD(逐对Δ)` 在同一对 step 上相减后再取中位数绝对偏差；`|base|×1%` 是相对下限。
- 正负方向不一致时（配对 ≥2 且一致率 < 0.75）判 `info`，不下优化/退化结论。

- `--min-delta-ms` 默认 0.05（与 v1 一致）。
- 组大小 < 3 时 MAD 不可靠，判定主要由 `--min-delta-ms` 决定；报告 `notes` 会标注。
- 噪声内不报优化/退化结论。

## 5. `--skip-warmup` 默认值为 0 的假设

单份采集的前几个 step 可能是真实负载的一部分（例如首个 prefill、冷启动后的第一次真实迭代），
自动跳步会静默改变用户显式选择的组；且不同负载的 warmup 长度差异很大。
因此默认 0，只有用户确认存在 warmup 时才显式指定，报告会记录实际跳过数量。

## 6. 指标与数据来源

| 指标 | 字段 | 来源 |
|---|---|---|
| `group_step_time_ms` | 优先 `STEP_TIME` 的 `endNs-startNs`；无则 `stage + preparing` | `ascend_pytorch_profiler_*.db` 优先，其次侧车 stage/prep |
| `group_computing_ms` | `computing` | `StepTraceTime` / CSV |
| `group_communication_ms` | `communication_not_overlapped` | `StepTraceTime` / CSV |
| `group_free_ms` | `free` | `StepTraceTime` / CSV |
| `group_preparing_ms` | `preparing` | `StepTraceTime` / CSV |

- 侧车时间单位为 µs，输出统一换算为 ms。
- 步耗时优先取 `STEP_TIME` 跨度（≈ stage+preparing），避免 `step_trace_time.csv` 的 Stage/Free
  与 `analysis.db` 不一致时同一采集得出不同结论；`stage+preparing` 与跨度偏差 >2% 写入 `notes`。
- 多 device 行：Stage/Preparing 取 max，Computing/Communication 求和，Free 按
  按 device 求和（与 Computing/Communication 同口径），不再由 stage 反算；因此分量之和可能大于步耗时。
- CSV 列名兼容大小写、空格与括号，例如 `Communication(Not Overlapped)`。

## 7. 降级与置信度

- 有完整四分类（Computing / Communication(未重叠) / Free / Preparing）→ `confidence=high`。
- 仅 `STEP_TIME` → 只算步耗时，四分类记 0 且 `verdict=info`（不是 `noise`），`degraded` 写「四分类缺失」，`confidence=low`。
- 侧车 step id 与 `STEP_TIME` 不一致：取并集，仅有 `STEP_TIME` 跨度的步保留并参与步耗时比较；该步缺侧车行时分量指标不可用（不参与判定）。
- 完全没有 step 数据 → `metrics` 为空、`summary="无可用 step 数据"`，退出码 0，不伪造数字。
- `StepTraceTime` 表存在但缺少部分四分类列 → 逐列记录，仅缺失指标不可用（展示 0、`verdict=info`），其余指标照常判定；同样标降级、`confidence=low`。

## 8. 与 v1「指定两步对比」的区别

| 维度 | 本 skill | v1 compare |
|---|---|---|
| 输入 | 1 份采集 | 2 份采集 |
| 对齐 | 组内按位置配对 | 跨采集按 step 位置配对 |
| 结论 | 单份内的坏 step / 阶段性劣化 | 跨采集瓶颈变化与迁移 |
| `mode` | 固定 `single` | `pair` |
| 步耗时 | 优先 `STEP_TIME` 跨度，缺省 `stage+preparing` | `stage+preparing` |

两者都使用 MAD 噪声带与中位数代表值，但结论不可互推：v1 不能回答“这份采集里哪个 step 最差”，
本 skill 不能回答“优化后整体是否变快”。
