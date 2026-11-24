---
name: kptools-ascend-profiling-host-dispatch
description: |
  统计单份 Ascend/torch_npu profiling 的 Host 下发耗时：per-op enqueue 时延分布
  （p50/p95/p99）、enqueue→dequeue 队列等待、下发→设备启动延迟、每 step 下发总量、
  下发与设备执行的重叠比、下发耗时/阻塞 Top 算子，并给出配对率与口径说明。
  当用户问“host 下发慢不慢、enqueue/launch 花了多久、下发到设备启动隔了多久、
  哪类算子下发阻塞、host 和设备的重叠度”时使用。
  两份 trace 的 enqueue 前后对比走 kptools-ascend-profiling-compare-enqueue/dequeue；
  通用 trace 分析走 kptools-ascend-profiling-analysis / -nostack；Free 构成走
  kptools-ascend-profiling-free-breakdown。
---

# Host 下发耗时统计

## 适用

- 判断 host 侧是否成为瓶颈：下发慢、下发阻塞、下发与执行重叠不足。
- 定位下发耗时或“下发→设备启动”延迟最高的算子。
- 量化每 step 的下发总量与 host/device 重叠比。

不适用：

- 两份 trace 的 enqueue/dequeue 差异 → `kptools-ascend-profiling-compare-enqueue` / `-dequeue`。
- Free 时间构成、气泡定位 → `kptools-ascend-profiling-free-breakdown` / `-compare-bubble`。
- 通用单份 trace 的阶段/四分类 → `kptools-ascend-profiling-analysis` / `-analysis-nostack`。

## 输入

| 输入 | 作用 |
|---|---|
| `trace_view.json` | `enqueue`/`dequeue` 事件（`correlation_id` 精确配对）、设备任务 |
| `ascend_pytorch_profiler_*.db` | `PYTORCH_API`（op/queue）、`CANN_API`（launch）、`TASK`、`COMPUTE_TASK_INFO`、`CONNECTION_IDS`、`STEP_TIME` |
| `kernel_details.csv` | 无 `STEP_TIME` 时的 step 划分 |

路径可以是 capture 目录、`ASCEND_PROFILER_OUTPUT` 目录或裸 `trace_view.json`。

## 工作流

```bash
python3 scripts/host_dispatch.py <capture|trace_view.json> \
  --json result.json --out-html report.html \
  [--skip-warmup K] [--pair-window-ms 2] [--top 10]
```

1. 默认自动选源：有 db 时优先 `PYTORCH_API → CONNECTION_IDS → TASK` 精确配对。
2. 无 db 或配对为空时，用 trace 的 `enqueue/dequeue(correlation_id)` + `dequeue → 设备任务` 时间窗最近邻启发式。
3. 报告里先看 `pair_rate`，再看 `dispatch_delay_*`；配对率低时延迟只代表可配对子集。

## 指标口径

| 指标 | 定义 | 来源 |
|---|---|---|
| `enqueue_p50/p95/p99_ms` | 单次 enqueue 事件时长分布 | trace `enqueue` 或 db `PYTORCH_API(type=50002)` |
| `queue_wait_*_ms` | dequeue 开始 − enqueue 结束 | trace `correlation_id` |
| `dispatch_delay_*_ms` | 设备任务开始 − 实际 enqueue(50002) 结束；无 queue 记录时回退 op 结束（口径见信封 `dispatch_delay_basis` 的 enqueue/op 计数与分位数） | db `PYTORCH_API(op/queue) → CONNECTION_IDS → TASK`；trace-only 为启发式 |
| `launch_*_ms` | `CANN_API` launch 调用时长分布 | db `CANN_API(type=10000/20000)`；无 db 时缺失 |
| `pair_rate` | 成功配对的 op 数 / enqueue 数（上限 1.0） | 同上 |
| `host_device_overlap_ratio` | enqueue 区间 ∩ 设备任务区间 / enqueue 总时长；设备区间取 trace 事件或 db `TASK.startNs/endNs`，都缺则不可用（null） | 区间运算 |
| `exposed_dispatch_ms` | enqueue 区间中不与设备任务重叠的时长，即下发阻塞/暴露在关键路径的时间；每 step 同口径 `exposed_ms` | 区间运算 |
| `per_op.top_enqueue[*]` | 每算子 enqueue 次数、总时长与 p50/p95/p99 | 按算子聚合 |
| `per_step.*` | 每 step 下发数、enqueue 总时长、延迟合计、重叠比 | 按 step 归并 |

- `--skip-warmup K` 会同步裁剪全局指标、Top、直方图与 per_step（同一统计窗口）；未指定时全局保留全量，但 `outside_window` 显式记录落在 step 窗口外、不参与 per_step 的事件数。
- 负延迟（设备在 enqueue/op 结束前已启动）单列在 `dispatch_delay_basis.*.negative` 与 `negative_excluded`，不混入分位数与 Top。
- 时间统一用 ns 计算，展示为 ms。
- `enqueue` 名称去掉 `Enqueue@` 前缀后作为算子名（如 `acl_memcpy_host_to_device`）。

## 输出

- 终端：一句话摘要 + 降级项。
- `--json`：附录 A.1 信封（`mode=single`）+ `per_step` + `per_op.top_enqueue/top_delay/top_launch` + `charts` 直方图分箱 + `dispatch_delay_basis`（enqueue/op 计数、负延迟计数、分位数）+ `outside_window`（step 窗口外事件数）。
- `--out-html`：判定指标、enqueue/下发延迟直方图、每 step 下发、下发耗时 Top、下发→启动延迟 Top、launch 耗时 Top、降级与口径。

## 降级路径

| 情况 | 行为 |
|---|---|
| 无 trace，仅 db（含直接传 `ASCEND_PROFILER_OUTPUT`） | enqueue 用 db queue 表；queue_wait 缺失并标注；重叠/暴露用 TASK 区间，TASK 也无起止时标不可用 |
| 无 db | trace `correlation_id` + 时间窗启发式；配对率写入报告 |
| enqueue/dequeue 无法配对 | `queue_wait` 记 0 并在 `degraded` 说明 |
| 无可靠 step（含可选 `detect_steps.py` 缺失/失败） | 整窗统计 + `degraded` 说明 |
| 配对率 < 30% | confidence 降为 medium/low，并显式告警 |
| trace 启发式配对 | confidence 最高 medium（即使配对率 ≥30%、步源是 kernel_details） |
| 输入不可读 | HTML 错误页 + 退出码 3 |

## 口径与限制

- `dispatch_delay` 包含排队等待（前面的 kernel 未执行完时，设备启动会被推后），不是纯 host 开销；判断 host 是否瓶颈应结合 `enqueue_*` 与 `queue_wait_*`。
- trace-only 的 dequeue→设备任务配对是时间窗最近邻，可能错配到相邻算子；配对率与 confidence 已标出。
- 多 device / 多 rank 时 `TASK.startNs` 取同一 connection 的最早启动，跨 rank 绝对时间不可直接比较。
- 本 skill 只分析单份采集，不做前后对比。
- 输出确定：同一输入、同一参数重复运行结果一致。
- 配对链路实现来源：`kptools-ascend-profiling-compare` v1（未合入时的复制版，口径见 `references/dispatch-definition.md`）。

## 依赖

- 仅 python3 标准库；配对与统计逻辑自带，运行时不依赖其他 skill。
- STEP_TIME 为空但 db 含 `PYTORCH_API/TASK/CANN_API` 时仍加载精确配对，步边界可来自 `kernel_details.csv`。
- 可选软依赖：`kptools-ascend-profiling-analysis-nostack/scripts/detect_steps.py`，仅用于无 `STEP_TIME` / `kernel_details.csv` 时的 step 边界兜底。加载失败、异常或返回空时自动退化为整窗统计并降级置信度，不影响运行。
- 配对链路为 `kptools-ascend-profiling-compare` v1 的复制实现（见「口径与限制」），不与 v1 脚本做相对路径引用。

## 参考资料

- [references/dispatch-definition.md](references/dispatch-definition.md) — 字段口径、配对链路、重叠比与置信度规则。

## 开发校验

```bash
python3 -m unittest tests.test_host_dispatch -v
```

合成夹具覆盖：db 精确配对数值、trace-only 启发式、配对失败降级、信封契约、per-step/overlap/exposed、launch 与 per-op 分位、直方图与 notes、空时长边界。
