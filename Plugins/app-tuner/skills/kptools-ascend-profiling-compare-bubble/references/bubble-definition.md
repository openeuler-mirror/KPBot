# 设备气泡定义与口径

## 1. 忙碌 / 空闲 / 气泡

- **设备忙碌区间**：同一采集内所有流的设备任务事件区间之并集。
  - 计算类：`cat`/`args["Task Type"] ∈ `{AI_CORE, AI_VECTOR_CORE, MIX_AIC, MIX_AIV, AI_CPU, DSA_SQE}`；
  - 拷贝类：`SDMA_SQE`（D2D）、`PCIE_DMA_SQE`（H2D/D2H）、`WRITE_VALUE_SQE`、`MEM_WRITE_VALUE`——拷贝期间设备在执行，不算空窗；
  - 通信：`cat == COMMUNICATION` 的事件，或名字以 `hcom` 开头的事件（无论 cat）；
  - 等待类（`NOTIFY_WAIT`/`EVENT_WAIT`/`NOTIFY_WAIT_SQE` 等）不视为忙碌，它们本身就是设备在等待；
  - 无设备任务事件时，依次回退：`Computing`/`Communication` 的 Overlap Analysis 区间（`cat=Overlap Analysis`，或真实 trace 中 `cat=None` 但仅靠进程名标识的情况）→ `StepTraceTime` 的 `Computing+Communication` 时长近似。
- **窗口范围**：`[第一个 kernel 开始, 最后一个 kernel 结束]`。窗口之外的静默区（采集启动/结束）不算气泡。
- **空闲窗口**：窗口范围减去忙碌并集。
- **气泡**：空闲窗口中时长 ≥ `--min-gap-ms` 的部分；`--max-bubble-ms` 可过滤异常长尾（默认不过滤）。

## 2. 三类判据（优先级从高到低）

1. `sync`（同步）：窗口内有 host synchronize 类事件。判据：事件名（小写）包含 `synchronize`，覆盖
   `Synchronize` / `Event::synchronize` / `aclrtSynchronize*`。
2. `comm_wait`（通信等待）：窗口紧邻（起点/终点 ±`--adjacent-ms`，默认 1ms）的 kernel 是通信类。
   判据：名字包含 `hcom`、`cat == COMMUNICATION`，或 Overlap Analysis 回退模式下的 `Communication` 区间。
3. `compute`（不可归因空窗）：其余气泡。表示设备没有任务执行，且不能归因到同步或通信邻接；
   仅说明“无法归因”，不证明是计算气泡，也不排除正常 idle（如等待外部输入、sleep），需要在报告层面按下限解读。

分类只描述“空窗紧邻/覆盖了什么”，不证明因果。需要因果时回看 trace 的 host/device 配对。

## 3. 「下发不足」为何 unsupported

第四类「下发不足」要回答“host 没及时把算子发下来，导致设备空转”，必须拿到 host 下发链路
（enqueue → launch → device start）与设备执行的配对数据，属于 T1-2 `kptools-ascend-profiling-host-dispatch`
的本批范围之外。缺少该数据时硬判会把“通信依赖等待”“同步等待”误归因成下发问题。
因此本批只输出三类，报告与 JSON 中把第四类标为 `unsupported` 并写明原因。

## 4. 与 v1 Free 的分工

| 问题 | owner | 口径 |
|---|---|---|
| 每步空了多少、Free 占比多少、瓶颈是否迁移 | `kptools-ascend-profiling-compare`（v1） | 步级 `Free = 步长 − (Computing ∪ Communication)`，中位数 + MAD |
| 空在哪个窗口、持续多久、属于哪类 | 本 skill | 设备窗口级补集 + 三类归因 |

两者结论不可互相替代：v1 的 Free 是**每步汇总口径**，本 skill 是**设备时间轴窗口口径**；
本 skill 明确不输出占比结论，v1 不列窗口清单。

## 5. 对比与噪声

- pair 模式下 `base/cand` 为 **per-step 中位数**；无 step 边界时 count 取气泡总数、max 取最长气泡、total 取总时长（与 `_series_for` 无 step 分支一致），
  `Δ = base − cand`，正值表示候选气泡更少/更短（更省）。
- 有 step 时按位置配对：`paired_delta = median(base_i − cand_i)`；
  时长类 `noise = max(--min-delta-ms, MAD(逐对 Δ))`，
  计数类 `noise = max(--min-delta-count, MAD(逐对 Δ))`（默认 1 个气泡，避免 1 个差异被判成改善/恶化）；
  verdict 由 `paired_delta` 与 noise 判定。**Δ 与 noise 同为 per-step 量纲，verdict 不随 step 数变化。**
- 无 step 边界时：Δ 为上述同一量纲的差值（count=总数、max=最长气泡、total=总时长）；时长类 `noise = max(--min-delta-ms, MAD(base)+MAD(cand))`，
  计数类 `noise = max(--min-delta-count, MAD(base)+MAD(cand))`，二者同为单气泡量纲。
- 与 v1 对比口径一致：v1 用逐对 Δ 的 MAD 消除步间方差，本 skill 在有 step 时采用同一规则。
- `|paired_delta| ≤ noise` 判 `noise`，不下优化/退化结论。
- `mode=single` 保留整窗聚合值，不参与判定；summary 的“整窗总时长/数量”是聚合值，
  与 metrics 的 per-step 中位数不同层级，不可混用。

## 6. 降级与置信度

- `high`：两份都是 native（有 `StepTraceTime` 侧车）且无降级。
- `medium`：trace-only，或存在任一降级。
- `low`：没有可靠 step 边界，`per_step` 为空；或输入不可读。
- **多 rank**：输入含多个 rank/capture 时只分析一个 rank（默认 rank 号最小，可用 `--rank` 指定），
  在 `notes`/`degradations` 写明，并把 confidence 上限压到 `medium`；`selected_rank`/`available_ranks`
  写入元数据，避免把单卡结论当成全卡结论。

## 7. 时间与单位

- 内部统一 ns；`trace_view.json` 的 `ts/dur` 按 µs 读入后 ×1000。
- `bubble_*_ms` 以 ms 输出，保留 4 位小数；HTML 时间轴与 Top 10 表给出绝对 ns 时间戳，便于回 trace 定位。

## 8. 检测复杂度与输出规模

- `busy_items` / `busy_comm` / `sync_events` / `steps` 在检测前预排序；每个气泡的同步覆盖、
  通信邻接、左右 kernel 名与 step 归属都用 `bisect` 只查邻域，不再逐气泡全量扫描。
- 整体复杂度 `O((B+N)·logN)`（B 为气泡数，N 为设备事件/步数）；per-step 聚合也改为一次分组，
  避免 `O(B×steps)`。
- 回归用例 `test_hundred_k_event_scale_is_fast` 覆盖 10 万忙碌区间 / 10 万气泡规模（阈值 5s）。
- 相邻 kernel 名只对 HTML Top-10 计算，其余气泡不做邻域查询；10 万事件/10 万气泡实测 <1s。
- 大 trace（>512MB）兜底改为逐对象 `raw_decode`：兼容任意键序与带引号 `dur`，Task Type 只从当前事件
  `args` 读取；解析事件数少于 `"ph":"X"` 标记数时写入 `degradations`，避免静默幻影气泡。
- 输出规模：JSON 的 `base/cand` 仅保留元数据（含 `window`），不写 `busy`/`steps`/`busy_items`/
  `sync_events`/逐气泡明细；`per_step` 保留契约字段。HTML 时间轴在段数 >2000 时按 800 色带聚合，
  `per_step` 表最多展示 200 行。10 万事件夹具的 JSON/HTML 均 <2MB（回归用例
  `test_json_and_html_output_are_bounded`）。
