# Host 下发口径与配对链路

## 1. 术语

| 术语 | 定义 |
|---|---|
| enqueue | host 把任务放入设备队列的调用，trace 中 `cat=enqueue`，名称形如 `Enqueue@acl_memcpy_host_to_device` |
| dequeue | 设备侧/驱动取出任务的时刻，trace 中 `cat=dequeue`，与 enqueue 共享 `correlation_id` |
| launch | runtime 的 kernel/拷贝启动 API，`CANN_API(type=10000/20000)` |
| device start | 设备任务真正开始的时刻，`TASK.startNs` |
| dispatch delay | 本次下发从 enqueue 结束到设备任务开始的时间，含排队等待 |

## 2. 原生侧车配对（优先）

链路：

```
PYTORCH_API(type=50001, name=aten/op)
  └─ connectionId ─┐
CONNECTION_IDS(id) ─┴→ connectionId ─→ TASK.connectionId
                                       └→ TASK.startNs / globalTaskId
                                          └→ COMPUTE_TASK_INFO.name
```

- `PYTORCH_API.type`：`50001` 为 op，`50002` 为 queue（enqueue）。
- `CONNECTION_IDS` 把 op 的 `connectionId` 映射到 `TASK.connectionId`；
  不同 CANN 版本可能缺少该表，此时直接尝试 `PYTORCH_API.connectionId == TASK.connectionId`。
- 同一 `connectionId` 可能对应多个 TASK（例如通信分解），取最早 `startNs` 作为 device start。
- `dispatch_delay` 优先用同 `connectionId` 的**实际 enqueue（type=50002）结束时间**：
  取不晚于 device start 的最晚 enqueue 结束；无 queue 记录时才回退到 op（50001）结束，
  口径写入信封 `dispatch_delay_basis.enqueue/op`（count、negative、p50/p95）。
  设备在 enqueue 返回后、op 返回前启动时会出现负延迟：这些样本单列在
  `negative` 计数并从分位数/Top 中剔除，不再把正常下发算成负延迟。
- 配对率 = 能解析到 TASK 的 op 行数 / enqueue 事件数；典型采集里只有真正落到设备的 op 能配对，
  因此不需要 100%。

`CANN_API` 中 `type=10000`（node）是 kernel launch，`20000` 是 acl 调用；
本 skill 用它的时长作为 launch 开销参考，不单独计入 dispatch delay。

## 3. 纯 trace 配对（降级）

1. enqueue 与 dequeue 用 `args.correlation_id` 精确配对：`queue_wait = dequeue.start − enqueue.end`。
2. dequeue → 设备任务：
   - 候选：`dequeue.start` 之后、`--pair-window-ms`（默认 2ms）之内的设备任务；
   - 取最早开始者；
   - `dispatch_delay = device.start − enqueue.end`。
3. 该启发式可能把相邻算子配错；配对率写入 `pair_rate`，`< 30%` 时报告显式告警。

## 4. 重叠比

`host_device_overlap_ratio = |enqueue 区间 ∪ 与设备任务区间的交集| / |enqueue 区间总长|`

- 1.0 表示下发期间设备一直在忙（下发被设备执行覆盖，隐藏了 host 开销）；
- 接近 0 表示下发时设备空闲（下发直接暴露在关键路径）。
- 设备任务区间：trace 设备事件优先；仅 db 时用 `TASK` 的 `startNs/endNs`；
  两者都缺时重叠比/暴露时长标为不可用（`null`），不按 0 计。
- 每 step 重叠比与整体口径相同。
- `exposed = enqueue 区间总长 − 与设备任务重叠部分`，即下发阻塞/暴露在关键路径的时长；
  整体给 `exposed_dispatch_ms`，每 step 给 `exposed_ms`，用于定位下发阻塞时段。

## 5. 置信度

| 条件 | confidence |
|---|---|
| db 精确配对且配对率 ≥ 30% | high |
| trace 启发式配对成功（即使配对率 ≥30%、步源是 kernel_details） | medium |
| 配对率 < 30%，或只有 enqueue 没有 dispatch | medium |
| 既无 enqueue 也无配对，或整窗统计 | low |

## 6. 常见误读

- `dispatch_delay_p95` 很大不等于 host 慢：设备队列积压时，任务会排队等待；应同时看
  `enqueue_*`（host 真正花的时间）与 `queue_wait_*`（驱动排队）。
- `enqueue_p50` 只有几微秒是正常的：enqueue 本身很轻，瓶颈常在下发频率、锁竞争或 host
  计算占用，而不是单次 enqueue 调用。
- 多 rank 采集里 `TASK.startNs` 是各自时钟域，不能跨 rank 相减。
