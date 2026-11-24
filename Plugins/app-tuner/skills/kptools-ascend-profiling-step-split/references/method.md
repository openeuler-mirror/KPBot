# Step 切分方法、口径与调参

## 1. 问题与目标

无栈 trace 上常见三种 step 信息：

1. `STEP_TIME`（`ascend_pytorch_profiler_*.db`）：权威 host step 边界；
2. `ProfilerStep#N`：trace 内权威标记；
3. 都没有：只能靠设备 busy/sync、host 活动和空档结构推断。

本 skill 的默认目标是**迭代粒度**：decode 场景下一次前向（一个 token）视为一个 step。
这通常比 profiler 粗窗口更细，因为一个 profiler step 可能覆盖多个迭代。

## 2. 数据预处理

- 设备任务：`ph=X`、`dur>0`、`pid != host_pid`，排除 Overlap Analysis 的
  `Computing/Communication/Communication(Not Overlapped)/Free`、`Synchronize*`、`ProfilerStep#`，
  且满足 `args["task id"]` 存在 / `task type` 为 AI 类 / 名称为 `Memcpy|Memset`。
- host_pid：`cpu_op/enqueue/dequeue` 事件最多的 pid。
- host 活动：该 pid 的 `ph=X` 事件，排除 `ProfilerStep#N` 与 Overlap Analysis 事件；
  `ProfilerStep#N` 只用于权威自检/`--authority`，不参与识别。
- 设备 busy 区间先做并集，再按小 gap 合并成**执行片段**：
  `merge_gap = max(0.1ms, 2 × 所有原始 gap 的中位数)`，但**达到切步阈值（`min_step_ns`）的空档永不合并**：
  否则相邻迭代空档都是 2ms 时 merge_gap 会变成 4ms，把所有边界候选吞掉。
  这一层避免把 kernel 之间的几十微秒调度抖动当成 step 边界。

## 3. 四个信号

### 3.1 idle（权重 0.55）

- 片段之间的空档 ≥ `min_gap_ns` 时，边界时间取**空档起点**（上一个片段结束）。
- 依据：STEP_TIME 的 step 起点通常紧贴上一个 step 的设备结束（实测差 < 0.3ms），
  而不是空档结束。
- 分数：`min(1, max(0.35, size / (3 × min_gap)))`，空档越大越可信。
- `min_gap_ns = max(0.3ms, --min-step-ms)`；默认 `--min-step-ms 0.5`，0.3ms 是硬下限。

### 3.2 sync（权重 0.30）

- 常规：只保留结束时间落在候选空档起点 ±`tol` 内的 `Synchronize*` 事件，
  用设备侧同步点佐证边界，同时避免把每个 step 内的 sync 都当边界。
- 兜底：当既没有 idle 候选也没有 host 候选时，若 sync 结束序列呈稳定周期
  （相邻间隔按 15% 容差聚类、至少 3 个点），则把与周期网格对齐的 sync 结束作为候选。
  该路径仍标 `uncertain`（单一信号）并在 `degraded` 注明“sync 兜底”。
- 分数：佐证固定 1.0；兜底按周期对齐误差在 0.3~1.0 之间。

### 3.3 period（权重 0.25）

- 取空档间隔中 ≥ `max(1ms, 3 × 空档中位数)` 的部分，按 15% 相对容差聚类；
  出现次数最多的簇（≥3 次）作为主导周期。
- 与周期网格对齐（误差 ≤ 15%）的空档起点成为候选；相位锚点为第一个空档。
- 用该周期处忙闲序列自相关作为置信度，`< 0.3` 视为无稳定周期。
- 说明：这是对 burst 序列做自相关的低成本等价实现，避免把 step 内 kernel 交替误判为周期。

### 3.4 host（权重 0.15）

- host 事件并集（排除 `ProfilerStep#N` 与 Overlap Analysis 事件）出现
  ≥ `max(min_gap, 0.5ms)` 的空闲，且恢复后 2ms 内至少 2 个 `enqueue`，
  视为一次下发批次；边界取该 host 空档的**起点**（与 idle 信号同一参考点），
  避免同一物理边界被 idle/host 两端各标一次而产生幻影 step。
- 仅在 host 存在明确“空闲→恢复”结构时生效；持续繁忙的 host 不参与投票。

## 4. 融合

1. 所有候选按时间排序，间隔 ≤ `tol` 的归为一簇。
   `tol = max(0.5ms, 6% × 主导周期)`（无周期时 0.5ms）。
2. 簇内同一信号取最高分；置信度 = `Σ 权重 × 分数`，上限 1.0。
3. 边界时间取簇内候选的中位数。
4. 相邻边界间隔 < `min_step_ms` 时保留置信度更高者。
5. 只有单一信号支撑的簇标 `uncertain=true`；置信度低于 `--min-confidence` 同样标 `uncertain`。
6. `per_step[i].confidence` 描述该 step 的**起始边界**；首步起点没有前一边界，
   记 0 并标 `uncertain`。权威模式窗口从**首个权威起点**开始（trace 早于首个 ProfilerStep/STEP_TIME
   的预热区间不计为一步），首步按权威边界记 1。总体置信度取各边界置信度均值。

## 5. 权威自检

- 权威来源优先级：`STEP_TIME` → `ProfilerStep#N` → `kernel_details.csv` 的 `Step Id` 最小起点。
- 对比时只剔除与窗口起点**重合**的权威标记；`--tolerance-ms` 仅用于后续最近匹配，不能把 1/2ms 等真实内部边界一并删掉。
- 贪心最近匹配，容差 `--tolerance-ms`（默认 2ms）；输出 precision / recall / F1 / 最大绝对误差。
- 自检结果会反向约束总体置信度：
  - `F1 < 0.5` → `low`；
  - `0.5 ≤ F1 < 0.9` 且原为 `high` → `medium`。
- 无设备任务（host-only 兜底）时总体置信度最高 `medium`，即使边界均值更高。
- 当本次边界数 > 权威步数 × 2 时，`notes` 提示“权威 step 较粗、本次为迭代粒度”。

## 6. 调参建议

| 目标 | 参数 | 说明 |
|---|---|---|
| decode 迭代粒度 | 默认 `--min-step-ms 0.5` | 一个小迭代一个 step |
| 与 profiler 粗 step 对齐 | `--min-step-ms 5~20` | 空档阈值同步放大，只保留大步间空闲 |
| 权威模式 | `--authority` | 直接采用 STEP_TIME/ProfilerStep 边界 |
| 关闭自检 | `--no-self-check` | 大 trace 上省时间 |
| 容差更严 | `--tolerance-ms 1` | 边界标定更敏感 |

## 7. 已知限制

- step 内的偶发长空闲（如等待外部输入）会被当作候选边界；报告以 `uncertain` 与
  较低的 `confidence` 体现，而不是静默丢弃。
- 多流完全重叠时，设备 busy 并集没有空档，先尝试 host 批次；若 host 也没有明确空闲-恢复结构，
  再用稳定周期的 `Synchronize*` 结束兜底。若 sync 也不成周期，退化为整窗。
- host-only 兜底会排除 `ProfilerStep#N`，因此同一 trace 加不加 profiler 标记不会改变识别结果；
  只有权威自检/`--authority` 会使用这些标记。
- 权威 `ProfilerStep` 只标记 profiler 窗口时，默认迭代切分的 F1 会偏低，这是**粒度差异**，
  不是算法错误；用 `--min-step-ms` 调到粗粒度后再对照。
- 不还原 Python 栈，不判断阶段语义（prefill/decode 见 `kptools-ascend-profiling-prefill-decode`）。
