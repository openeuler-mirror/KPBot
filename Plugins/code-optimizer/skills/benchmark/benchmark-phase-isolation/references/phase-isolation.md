# 相位隔离全量（标定案例）

## 1. 问题形态

LOCOMO trace-stress-replay 默认 mixed：20 用户 / 20 并发，每用户"先 19 条 ingestion 再 199 条 QA"串行、
用户间并发。结果是 ingestion 与 qa 墙钟重叠，`overall` 的吞吐/时延是写读混合体，无法单独判定"写慢还是读慢"。

## 2. 三种相位模式

| 模式 | 入口 | 默认 | 语义 |
|---|---|---|---|
| mixed | `TRACE_REPLAY_PHASE_MODE=mixed` | ✓ | 零行为变更 |
| barrier | `TRACE_REPLAY_PHASE_MODE=barrier` | — | 全量 ingestion → 索引就绪屏障 → 全量 qa |
| 单相位 | `--phase ingestion\|qa` | — | 与 `--user-indices` 分片 lane 合并为同一语义 |

summary 增 `phase_windows`（各模式都有）；barrier 下另有 `phase_mode` / `index_settle` / 分相位 requests。

## 3. 分片 barrier 的去中心化

多 runner 分片下，全局屏障不能靠"等单一协调者"，改为**每片独立轮询同一全局谓词（全部 collections 的索引就绪）**：
全部就绪才放行该片，所有片最终必然到达（无死锁，已论证）。这是"去中心化全局 barrier"的标准做法，可迁移到任何分片写入场景。

**但不能"看到某个 collection 就绪就放行该片"**：快分片看到 collection 就绪时，慢分片可能仍在写；一次轮询
看到全部索引就绪，也挡不住后续写入。正确顺序：
1. 先等本轮**所有分片**确认 ingestion 完成（逐片报告"写完了"，而非只等索引状态）；
2. 再核对全部目标索引覆盖**最终写入**（`loading_progress` 100% + 真实 search 试探，无未落盘动态行）；
3. 最后放行 qa。任一分片失败或超时即 **中止本轮**，不产出数据点。

## 4. AsyncJsonlWriter

- 之前：每事件"加锁 + 开关文件"写 JSONL。
- 之后：后台线程批量写**逐请求事件** JSONL，替代高频小写。
- 价值：把高频小写移出请求关键路径（对被测吞吐影响可忽略，必要时以无事件落盘轮对照验证）。
- **相位标记不在 JSONL 里**：相位标记按阶段标记契约打到 **stdout**（`kind`/`phase` 字段 + `flush=True`，
  契约见 `opt-measurement-infra`，独立 PR 待合入），采集器 `tail -F run.log` 监听 stdout 起停。
- **定界要用事件发生时间**：JSONL 后台队列积压时 `flush` 只刷写线程已取出的事件，`tail` 看到某事件的时间
  晚于实际发生时间；`phase_windows` 的 start/end 取回放内记录的时刻（occurrence time），不取 JSONL 落盘墙钟。

## 5. 与统计设计的衔接

相位隔离给出干净的 `phase_windows`，统计设计（`benchmark-statistical-design`，独立 PR 待合入）在此基础上：
- 主口径锁定"可变相位（qa）的 retrieval 吞吐 + p50/p95/p99"。
- seed + 索引构建是恒定开销（10–20 分钟），不进对比。
- 精确窗口用 `phase_windows` 复核，不假设边界干净。

## 6. 坑点

- **mixed 非 barrier**：未设 `TRACE_REPLAY_PHASE_MODE`（默认 mixed）时两相位墙钟重叠，单看 overall 会误判。
- **无 index_settle（按 phase_mode 区分必需字段）**：mixed 下唯一 `stable_polls` 来自 seed 后的 index 等待，
  **barrier 才有 `index_settle`**。验收门禁必须按 `phase_mode` 列出必需字段，不能对所有模式都要求
  `index_settle`，否则正常 mixed 回放也会验收失败；单相位运行只检查所选相位窗口。
- **分片 barrier 别用全局时钟**：分片写入下唯一可靠的是"先等全部分片 ingestion 完成，再每片 wait 全部
  collections 并核对覆盖最终写入"（三步顺序见 §3，索引谓词单独不可靠）。
- **tail 标记 ≠ 相位边界**：队列积压时 tail 看到的标记晚于实际切换，用事件发生时间定界。