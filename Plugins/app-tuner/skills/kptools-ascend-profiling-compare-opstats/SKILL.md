---
name: kptools-ascend-profiling-compare-opstats
description: |
  专项对比：两份 Ascend/torch_npu profiling 的算子维度差异——算子种类数、每步调用次数、
  shape 漂移、单算子时延分位数（p50/p95/p99）、新增/消失算子清单。
  当用户明确要看“算子种类/数量/shape/分位数怎么变了”“哪些算子新增或消失”时使用。
  通用“优化前后差多少 / 瓶颈变了吗”走 kptools-ascend-profiling-compare；
  单份 trace 分析走 kptools-ascend-profiling-analysis（有栈）或 -analysis-nostack（无栈）；
  设备气泡/stream/拷贝/融合分别走对应的 compare-bubble / stream / h2d-d2h / fusion。
  注意：上述专项对比 skill 由后续 PR 提交，当前仓库可能未安装；未安装时如实说明
  “暂无对应 skill，需人工分析”，不要臆造结果。
---

# 算子统计对比（opstats）

## 适用范围

- 同一负载的两份采集（优化前后、两个配置、两个版本），关注**算子维度**。
- 只回答：种类、次数、shape、时延分布、集合变化。
- 不回答：步耗时与四分类（v1 `kptools-ascend-profiling-compare`）、气泡（bubble）、流结构（stream）、拷贝（h2d-d2h）、融合证据（fusion）。这些专项对比 skill 由后续 PR 引入，当前仓库可能未安装；缺失时如实说明暂无对应 skill。

## 输入

每个采集支持：

1. `*_ascend_pt` / `ASCEND_PROFILER_OUTPUT` 目录（优先 `kernel_details.csv`）
2. `ascend_pytorch_profiler_*.db`（`TASK` JOIN `COMPUTE_TASK_INFO`，名字/shape 经 `STRING_IDS` 解码，step 用 `STEP_TIME`）
3. 裸 `trace_view.json`（设备事件兜底）

数据源优先级：`kernel_details.csv` → db `TASK+COMPUTE_TASK_INFO` → trace 设备事件。实际用到的文件写入 `data_quality.sidecars`。
两侧数据源不同会写入 `degraded`，并降置信度到 `medium`（口径不可直接比较；按数据源类型判断，多个 db 文件不算跨源）。

- 多 db：按 `STEP_TIME` 行数择优（与 `analysis-nostack` 一致），其余未纳入会写入 `notes` 与 `degraded`。
- CSV 存在但不可解析：`degraded` 记录「解析失败（原因）」，与「文件缺失」区分。
- trace：兼容 `list` 与 `{"traceEvents":[...]}`；超过 512MB 只解析前 128MB 的完整事件并降级。
- 通信算子 `hcom_*` 按名称前缀优先保留，不受 `Task Type`（如 `COMM_SQE`）影响。

## 工作流

```bash
python3 scripts/opstats.py <base> <cand> --out-html report.html --json result.json \
  [--min-delta-ms 0.05] [--min-delta-count 0] [--rename-jaccard 0.6]
```

读报告顺序：Hero 结论 → 指标对比表（Δ/噪声/判定）→ 新增/消失/疑似改名 → shape 变化 → 降级说明。

## 指标与口径

| 指标 | 单位 | 含义 |
|---|---|---|
| `op_kind_count` | count | 算子名去重数（`verdict=info`） |
| `op_call_count_per_step` | count | 每步调用次数（总数 / 步数） |
| `op_time_ms_per_step` | ms | 每步算子总耗时 |
| `op_p50_ms` / `op_p95_ms` / `op_p99_ms` | ms | 单算子每步耗时在全部 (算子, step) 样本上的分位数 |
| `op_new_count` / `op_gone_count` | count | 集合差异（`verdict=info`） |
| `op_shape_changed_count` | count | 同算子 shape 集合（Input Shapes + Output Shapes）变化个数（`info`） |
| `op_shape_unparsed_count` | count | shape 缺失/无法解析的行数（`info`） |

- `Δ = base − cand`，正值表示候选更省；时间类与每步调用次数参与方向判定（计数噪声下限 `--min-delta-count`），新增/消失/shape 等集合类指标只给 `info`。
- 噪声带：时间类 `max(--min-delta-ms, MAD)`，每步调用次数 `max(--min-delta-count, MAD)`（序列为 step 或分位数样本）；`|Δ| ≤ noise` 判 `noise`。
- 改名判定：名称分词后 Jaccard ≥ `--rename-jaccard`（默认 0.6）视为疑似改名，**不计入**新增/消失，写入 `notes`。
- `confidence`：两侧都来自 `kernel_details.csv` 且无降级 = `high`；db/trace 回退或任一降级 = `medium`；无 step 信息（整窗聚合）= `low`。

## 输出

- 终端摘要、A.1 九字段 JSON、HTML（指标表、新增/消失/疑似改名、shape 变化、降级说明与口径）。

## 降级路径（始终产出报告）

1. 无 `kernel_details.csv` → db `TASK+COMPUTE_TASK_INFO`（仅计算内核，不含通信/拷贝），`degraded` 记一条。
2. 无侧车 → trace 设备事件，`confidence=medium`。
3. shape 列缺失或值空 → shape 指标记 0 / 计入 `unparsed`，不影响其余指标。
4. 无 step 信息（缺 `ProfilerStep#N` / `STEP_TIME`，或 CSV `Step Id` 列缺失/全空）→ 整窗聚合，`confidence=low`，`notes`/`degraded` 说明；CSV `Step Id` 部分为空时记 `notes` 并归 step0。
5. 输入不可读 → 输出 HTML 错误页，退出码 3。

## 真实数据口径

- 真实数据优先 `kernel_details.csv`（含 `Input Shapes`/`Output Shapes`，形如 `"1;"`）；两者都纳入 shape 集合。
- 读取层会把通信算子实例后缀归一化（`hcom_x__503_98_1` / `hcom_x_98_1` → `hcom_x`），`aggregate()` 归类前再做一次防御归一化；因此 `op_kind_count` 与新增/消失都不会被实例下标拆散，同类多实例（base 1 个、cand 多个）也不计新增；归一化条数写入 `notes`。
- 时间单位 µs；无 step 信息时整窗聚合。
- db 路径经 `COMPUTE_TASK_INFO` 只覆盖计算内核；`kernel_details.csv` 还包含通信任务，跨数据源比较不可当收益。
- 「每步」分母使用完整已知 step 集合：已知但本侧没有算子命中的 step 补零计入，避免样本少时每步值被放大。
- 两侧任务类型覆盖不同（如 base 含通信任务 CSV、cand 仅计算 db）时，次数/耗时/分位数指标标 `info`、不做方向判定，并在 notes 说明。

## 限制

- 不还原调用栈，不做算子内部并行度分析。
- shape 字段的可用性依赖 CANN 版本；解析失败不丢弃算子。
- 分位数按 (算子, step) 样本计算，样本量少时仅供参考（`notes` 标注样本数）。

## 开发校验

```bash
python3 -m unittest tests.test_profiling_opstats -v
```
