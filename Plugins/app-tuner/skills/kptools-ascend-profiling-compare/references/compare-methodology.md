# Profiling 对比方法学

## 1. 对齐

### 两份采集之间

- 默认按 step 位置配对（base 第 i 个 step ↔ cand 第 i 个 step），显示两侧 step id。
- 步数不一致时只比交集，并在报告“数据一致性”里写明。
- 支持显式指定：
  - `--base-step 3 --cand-step 7`：单步深对比；
  - `--base-steps 1,2 --cand-steps 1,2`：组内聚合后比。
- **禁止**按时间戳排序后配对，也**禁止**跨采集 raw ts 相减。

### 单份采集内

- 时间都取自同一份采集的 epoch，不跨采集、不跨 rank 相减。
- 本 skill 对 `ascend_pytorch_profiler_*.db` 的读取范围**仅 `STEP_TIME`**；不做 host→device
  配对，也不读取其他 host/device 配对表。
- trace 路径只使用事件自带的 `ts`/`dur`，不做全局时钟平移或 host/device 时钟校准。
- host 下发与设备启动之间的配对分析不属于本 skill 范围。

## 2. 度量

- 时间统一换算为 ms 展示；内部保存原生单位（StepTraceTime/kernel 为 µs）。
- 每步先算：`dur`、`comp`、`comm_no`、`free`、`prep`。
- **步耗时 `dur = stage + preparing`**：`StepTraceTime.stage` 只覆盖设备活跃窗口，`preparing` 是同一 step 内的准备期；两者相加才等于 `STEP_TIME` 的 step 跨度（真机 4 个模型、24 份采集实测误差 < 1%）。纯 trace 路径的 `dur` 取 step 边界跨度，`prep` 取 step 起始到首个忙碌区间的空隙。
- 分量可加性 `dur = comp + comm_no + free + prep` **只在单卡口径下成立**。native 的 `free` 直接取侧车值（= stage − comp − comm_no）；trace 路径的 `free = dur − busy − prep`，避免把准备期重复计进 Free。
- 多 `deviceId` 的 sidecar：按 step 取 **Stage 最大的卡作为主卡**，只保留该卡的 Computing/Communication/Free/Preparing（Free 仍按 `stage − comp − comm_no` 校验），从而保持可加性；其余卡的耗时**不纳入分量对比**，报告 `caveats` 明确写出主卡口径。
  - 不采用“Computing/Communication 求和 + Stage 取 max”：以 2 卡、每卡 stage=100/comp=80 为例，分量合计会到 155% 步耗时，占比与主导分量判定失真。
- 分量可加性由 `_components_additive` 逐 step 校验：任一侧 `|comp + comm_no + free + prep − dur| > max(1µs, 2%·dur)` 时，降级为只比步耗时，分量仅展示数值、不做判定。
- 通信数据：`communication.json` 优先；缺失时读 `analysis.db:CommAnalyzerTime`，按 step 汇总 elapse/wait/transit/sync（单位 ms），并把实际读取来源写进 `data_quality.sidecars`。
- 是否带通信数据由采集上的 `has_comm` 显式标志表示；整窗折叠时无通信侧车不写入零值 `comm[0]`，避免把“缺失”当成“通信为 0”。仅一侧有通信数据时仍出表，但必须在 `caveats` 写明缺失侧按 0 展示、不能作为优化结论。
- 多步汇总：中位数用于展示，逐对 Δ 的中位数用于判定，均值用于总量展示。
- 分量占比 = 分量 / 步长。

## 3. 噪声与判定

先按 step 配对求逐对差，再做判定（配对已由对齐保证）：

```
d_i = base_i − cand_i                   # 正 = 候选更省
Δ   = median(d_i)                       # 判定用的鲁棒差值
noise = max(min_delta_ms, min_delta_pct × base_median, MAD(d_i))
Δ >  noise  -> 改善
Δ < -noise  -> 恶化
否则        -> 在噪声内
```

- 默认 `min_delta_ms = 0.05`、`min_delta_pct = 1.0`，可用 `--min-delta-ms` / `--min-delta-pct` 调整。
- 用逐对差而不是 `MAD(base)+MAD(cand)`：后者会被 workload 自身的步间方差主导，且会随收益同比例增长，导致“所有 step 均匀快 15%”被判成噪声。
- 相对下限 `min_delta_pct × base` 防止把 0.4% 的抖动当成收益。
- **方向不一致**：若 ≥2 对 step 且同向占比 < 75%，判“方向不一致”（`verdict=info`），不下改善/退化结论；典型场景是 prefill/decode 双峰混在一起。
- 算子差异默认 `--min-kernel-ms = 0.02`；只在单侧出现的算子单独列出，不参与快慢排名。

## 4. 瓶颈迁移

- 分别取两侧选中的 step 集合，计算 Computing / Communication / Free / **Preparing** 的占比中位数。
- 占比分母是步耗时（含 `preparing`），所以主导分量必须包含 Preparing；否则 prefill 等大 Preparing 采集会把
  主导分量误判成 Computing/Free，且各项占比加不到 100%。
- 主导分量 = 占比最大者。
- 仅当步耗时判定为改善/恶化（非噪声、非方向不一致）且主导分量改变时，报告“X → Y”；方向不一致时不下迁移结论。
- 主要变化分量 = 改善/恶化分量中 |Δ| 最大者。
- 四分类来源跨族（CANN 侧车 vs trace 自建）、任一侧取不到设备任务分类（`class_source=none`）、或分量不满足可加性时，只比较步耗时，分量不做判定。

## 5. 降级路径

| 情况 | 行为 |
|---|---|
| 有 `ASCEND_PROFILER_OUTPUT` | 直接读 CANN 侧车 |
| 只有 `trace_view.json` | `build_index` 建索引 + `detect_steps` 切 step |
| 无 `ProfilerStep#N` 且无栈 | 设备 sync / idle gap；四分类按设备任务类型自建 |
| 取不到设备任务分类 | `class_source=none`，只比步耗时，分量仅展示数值 |
| 多 `deviceId` | 取 Stage 最大的主卡口径（分量可加），其余卡不纳入分量，caveat 说明 |
| 分量不满足可加性 | 只比步耗时，分量仅展示数值、不做判定 |
| 缺少 nostack 依赖（`detect_steps.py` 等） | trace 事件解析与自建分类跳过，仍用 `build_index` 侧车出报告并写入降级说明 |
| 切不出 step | 整窗对比（总时长/分类/算子/通信/API），报告显著标注 |
| 输入不可读 | 输出 HTML 错误页 + 退出码 3 |

## 6. 报告口径

- 所有 Δ 都是 `base − cand`；正文同时给 `cand/base` 倍率，回归时不再用 >100% 的百分比误导。
- 算子差异按选中的 step 数归一化到“每步”；通信算子实例下标（`hcom_allReduce__503_0_1`）归一化为算子名后再比较，只在单侧出现的算子单独成节、不参与排名。
- Host API 侧车是整份采集聚合，按采集总步数归一化，不随 `--base-step/--*-steps` 过滤，报告会标注。
- 通信差异按每步均值。
- 报告必须写清：step 来源、四分类来源、是否降级、是否同栈、版本/rank。

## 7. 输出契约（统一 JSON 信封 v1）

本节是该契约在**本 PR 内的权威定义**；`scripts/envelope.py::validate` 是同一契约的可执行校验实现，
也可直接运行 `python3 scripts/envelope.py result.json`（0 = 合法，1 = 不合法，2 = 读取失败）。
下游 hub 只消费 `metrics` / `summary` / `data_quality`，`per_step` 供深挖。

### 7.1 结构

```json
{
  "skill": "kptools-ascend-profiling-compare",
  "schema_version": 1,
  "mode": "pair",
  "inputs": {"base": "/path/base", "cand": "/path/cand", "steps": "mode=aligned, pairs=4"},
  "data_quality": {
    "sidecars": ["analysis.db", "communication.json"],
    "degraded": ["至少一侧没有可靠 step 边界，已退化为整窗对比"],
    "confidence": "high"
  },
  "metrics": [
    {
      "name": "step_time_ms",
      "unit": "ms",
      "base": 12.3,
      "cand": 11.1,
      "delta": 1.2,
      "delta_pct": 9.76,
      "verdict": "improved",
      "noise_band": 0.4,
      "paired_delta": 1.2,
      "sign_consistency": 1.0,
      "evidence": "step_source: STEP_TIME / STEP_TIME"
    }
  ],
  "per_step": [{"step": 0, "cand_step": 0, "base": 12.3, "cand": 11.1, "delta": 1.2}],
  "summary": "候选更快：步耗时 9.8%",
  "notes": ["噪声判定: noise = max(...)"]
}
```

### 7.2 字段约束

| 字段 | 类型 | 约束 |
|---|---|---|
| `skill` | string | 非空，固定 `kptools-ascend-profiling-compare` |
| `schema_version` | integer | 固定 `1` |
| `mode` | string | `pair` / `single`；本 skill 两份采集一律 `pair` |
| `inputs.base` / `inputs.steps` | string | 非空；`steps` 为选择说明 |
| `inputs.cand` | string / null | `mode=pair` 时必须非空 |
| `data_quality.sidecars` | string[] | 只列**实际读取成功**的侧车，不列探测失败的 |
| `data_quality.degraded` | string[] | 与 HTML 降级说明同源 |
| `data_quality.confidence` | string | `high` / `medium` / `low` |
| `metrics` | object[] | 非空；本 skill 固定五项（见 7.3），`name` 不重复 |
| `metrics[].unit` | string | 非空，本 skill 为 `ms` |
| `metrics[].base` / `cand` / `delta` / `delta_pct` / `noise_band` | number | 必须自洽，见 7.3 |
| `metrics[].verdict` | string | `improved` / `regressed` / `noise` / `info` |
| `metrics[].paired_delta` | number / null | 逐对 Δ 的中位数 |
| `metrics[].sign_consistency` | number / null | `[0,1]`，同向步占比 |
| `metrics[].evidence` | string | 非空，指向侧车/表/字段 |
| `per_step` | object[] | 每项含整数 `step`/`cand_step` 与数值 `base`/`cand`/`delta` |
| `summary` | string | 非空，一句话结论 |
| `notes` | string[] | 口径、限制、噪声规则 |

### 7.3 数值与判定口径

- `metrics` 固定五项与顺序：`step_time_ms`、`computing_ms`、
  `communication_not_overlapped_ms`、`free_ms`、`preparing_ms`。
- `delta = median(base) − median(cand)`，**正 = 候选更省**。
- `delta_pct = delta / median(base) × 100`；`median(base)=0` 时为 0，回归时绝对值可 >100%。
- `verdict` 由逐对 Δ 的中位数 `paired_delta` 判定，不由 `delta` 直接判定：
  `noise_band = max(min_delta_ms, min_delta_pct × |median(base)| / 100, MAD(逐对 Δ))`；
  `paired_delta > noise_band → improved`，`< −noise_band → regressed`，否则 `noise`；
  步间同向占比 < 75%（≥2 对）或分量不可比时 `info`。
- `per_step[].delta = base − cand`，逐对值，供复算 `paired_delta`。
- `verdict` 映射：改善 → `improved`、恶化 → `regressed`、在噪声内 → `noise`、方向不一致/不可比 → `info`。

### 7.4 可执行校验

`envelope.validate(env)` 检查：必填字段、枚举、类型、`base−cand` 与 `delta` 的一致性、
`delta_pct` 与 `delta/base` 的一致性（base ≥ 0.1ms 时，容差 `max(0.5, 2%×|pct|)`）、
`per_step[].delta` 一致性、`sign_consistency ∈ [0,1]`。CLI 入口：

```bash
python3 scripts/envelope.py result.json
```

## 8. 复用边界

- `scripts/kpc_core.py` 承载读取与度量（`load_capture`、侧车解析、区间运算、MAD、`_metric_block` 等），CLI 与差异计算在 `scripts/compare.py`。
- 其他 skill 复用前提是本 skill 已合入 master；未合入时只能复制实现并注明来源。
- 复用方不得就地修改 core 的度量口径；如需变更，先在本 skill 内改并补契约测试。
