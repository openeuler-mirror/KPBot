# opstats 口径与判定规则

## 1. 数据源与优先级

| 优先级 | 来源 | 提供 | 局限 |
|---|---|---|---|
| 1 | `kernel_details.csv` | 每步每算子名称/类型/耗时/shape | 列名随 CANN 版本变化，需按关键字匹配 |
| 2 | `ascend_pytorch_profiler_*.db`：`TASK` JOIN `COMPUTE_TASK_INFO`，名字/shape/类型经 `STRING_IDS` 解码，step 用 `STEP_TIME` | 算子名/耗时/类型/Input+Output shape | 只覆盖计算内核（不含通信/拷贝）；缺 `STEP_TIME` 时整窗 |
| 3 | `trace_view.json` 设备事件 | 名称/耗时/时间戳 | 无 shape；DMA SQE 属拷贝维度，不计入；落在 ProfilerStep 窗口外的事件剔除 |

多 Device 场景按 step 求和。实际用到的来源写入 `data_quality.sidecars`（只记文件名）。

**名称归一化（读取层 + 归类前防御）**：通信算子形如 `hcom_<op>__<数字>_<数字>...` 或
`hcom_<op>_<数字>_<数字>...` 的实例下标，在读取时去掉并统一为 `hcom_<op>`；`aggregate()` 归类前
再做一次 `norm_name` 防御，避免直接构造 cap 或新数据源漏归一化时，把同类多实例（base 1 个、
cand 多个）算成新增算子。未配上的 `hcom*` 若与 base 某算子 token 集相同，也按同类实例豁免。
否则同一类别会因实例下标不同被拆成多个 `op_kind`，两份采集之间也无法直接比较；归一化条数写入
`notes`。该口径与 `kptools-ascend-profiling-compare` 的 `kpc_core` 一致。

其他读取口径：

- 多 db：按 `STEP_TIME` 行数降序择优，采用第一个能产出行的 db；其余未纳入写入 `notes` 与 `degraded`。
- 跨源判定：按数据源类型（csv / db / trace）比较，相同类型的不同 db 文件名不算跨源。
- CSV 存在但表头/列/数据行不可用：`degraded` 记录 `解析失败（原因）`，与「文件缺失」区分。
- trace：兼容 `{"traceEvents":[...]}`；超过 512MB（`MAX_TRACE_BYTES`）只解析前 128MB 的完整事件并降级；`hcom_*` 按名称前缀优先保留，`Task Type=COMM_SQE` 也计入。

## 2. 指标定义

- `op_kind_count`：算子名去重后的数量，两侧各算一次，不做 Δ 方向的优劣判定（`info`）。
- `op_call_count_per_step`：`总调用次数 / 步数`；两侧步数不同时先各自归一，避免总时长差异被步数放大。
- `op_time_ms_per_step`：`总耗时(µs) / 步数 / 1000`。
- `op_p50/p95/p99_ms`：样本集合 = 每个 (算子, step) 的耗时（µs → ms）；线性插值分位数。
- `op_new_count` / `op_gone_count`：集合差；疑似改名的先剔除（见第 3 节）。
- `op_shape_changed_count`：两侧都出现且 shape 都可解析、但 shape 集合（Input Shapes + Output Shapes）不同的算子数。
- `op_shape_unparsed_count`：shape 为空/列缺失的行数，两侧分别统计。

## 3. 改名判定

1. 名称归一化：非字母数字转空格，驼峰拆分，转小写，去掉纯数字 token 与 token 尾部数字。
2. 取两侧 token 集合的 Jaccard 相似度；`≥ --rename-jaccard`（默认 0.6）判为疑似改名。
3. 疑似改名不计入新增/消失，写入 `notes` 与 HTML 表格；一个旧名只能匹配一个新名（按名称排序后贪心匹配）。

## 4. 噪声与判定

```
noise = max(--min-delta-ms, MAD(base 的每步序列) + MAD(cand 的每步序列))
Δ >  noise -> improved
Δ < -noise -> regressed
否则       -> noise
```

- 计数类指标（种类/新增/消失/shape）固定 `info`，避免把“算子变少”直接当成收益。
- 时间类指标（每步耗时、分位数）方向明确：候选更省 = `improved`。
- 分位数（p50/p95/p99）的 MAD 来自 (算子, step) 样本，同样受 `--min-delta-ms` 下限约束。
- 步数 < 3 时 MAD 不可靠，主要由 `--min-delta-ms` 决定（报告 `notes` 标注）。
- 摘要在耗时/次数都在噪声内时仍会带出「种类/新增/消失」；集合差异不会被静默丢掉。

## 5. 降级与置信度

| 情况 | 行为 | confidence |
|---|---|---|
| 两侧 `kernel_details.csv` 正常 | 全指标可用 | high |
| db / trace 回退 | shape 指标不可用，其余正常 | medium |
| 两侧数据源不同 | 集合/次数/耗时口径不同，写入 `degraded` | medium（不高于） |
| 无 step 信息（缺 `STEP_TIME` / `ProfilerStep#N`，或 CSV `Step Id` 列缺失/全空） | 整窗聚合，`per_step` 只有 1 条 | low |
| 输入不可读 | HTML 错误页 + 退出码 3 | — |

CSV `Step Id` 列存在但部分为空：记 `notes`（按 step0 归属的行数），不单独降置信度。

## 6. 与相邻 skill 的边界

以下相邻 skill 由后续 PR 提交，当前仓库可能未安装；未安装时应如实说明暂无对应 skill，不要臆造结果。

- 步耗时、四分类、瓶颈迁移：`kptools-ascend-profiling-compare`（v1，后续 PR）。
- 设备气泡：`kptools-ascend-profiling-compare-bubble`。
- 流结构：`kptools-ascend-profiling-stream`。
- 拷贝：`kptools-ascend-profiling-compare-h2d-d2h`。
- 融合证据：`kptools-ascend-profiling-compare-fusion`。
- 调用栈：`kptools-ascend-profiling-compare-stack`。
