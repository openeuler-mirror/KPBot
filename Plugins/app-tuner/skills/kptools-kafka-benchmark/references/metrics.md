# 原生指标、计时与正确性口径

## 测量范围

这是**单 producer、合成字节消息、指定正限速下的 producer-only** 测量，不是
consumer 吞吐、应用端到端延迟、集群容量认证或 TPC 类基准。官方
`ProducerPerformance` 每轮独立运行，配置和原始最终输出均保留。正限速下的
records/s 是受计划约束的观察值，不得写成 broker 最大吞吐。

## 每轮字段

`warmup` 与 `rounds[]` 使用同一结构但严格分开：

- `requested_records`：本轮请求消息数；`exit_code` 和 `timed_out` 是实际进程结果。
- `native.records_sent`：官方最终汇总中成功回调计数，不从退出码推断。
- `native.records_per_second`：官方整个进程测量窗口的 records/s。
- `native.megabytes_per_second`：官方字段名为 `MB/sec`，但 4.4.0 实现除以
  `1024 × 1024`；JSON 聚合字段因此明确命名为 `mebibytes_per_second_median`。
- `native.average_latency_ms`、`max_latency_ms`、`p50/p95/p99/p99_9_latency_ms`：
  官方从 send 到成功回调的毫秒统计。分位数使用其实现的采样数组，不是应用
  end-to-end 百分位。
- `process_wall_duration_ms`：本脚本对该官方 CLI 子进程的单调钟 wall 时间，
  包含 JVM/工具开销；与 native 内部窗口不同，不混用。
- `offsets_before/after`、`offset_delta`：各分区 latest offset 原始映射及总和差。
  它不是 consumer group committed offset。

预热成功但不进入 `aggregates`。`aggregates` 只含全部成功的计时轮，报告总消息数、
总载荷字节和各轮原生值的中位数。`p95/p99` 是“各轮分位数的中位数”，不是把
不同轮次样本池化后的新分位数。逐轮值和日志用于复算，不用单个中位数替代原始
证据。

## 精确正确性门

压测前，console producer 写入三条带 run UUID 的不同消息，随机 group 从头消费
三条；数量必须为 3 且内容集合逐字相等，跨分区不假设全局顺序。

每个 perf 阶段使用官方 `--payload-monotonic`，payload 是该阶段从 0 开始的十进制
字符串。脚本要求 `native.records_sent == requested_records` 且
`offset_delta == requested_records`，然后用新的随机 group 从头消费当前 topic
全部消息，把实际内容与“三条正确性消息 + 所有已完成阶段的十进制身份”做精确
多重集比较。不同阶段都从 0 开始，因此同一身份应出现的次数也属于校验；少一条、
多一条、错误身份或重复次数不符均失败。每阶段 `identity_validation` 保存期望/
实际总数、唯一身份数、missing/unexpected 数量与至多 20 个示例。最终 latest
offset 总和必须等于 `3 + warmup_records + rounds × records`。零退出码不能替代
任一检查；差异、缺失最终原生汇总、后续失败或不完整轮次都不会进入成功聚合。

`timed_payload_bytes` 按每个计时轮的 `sum(len(str(i)))` 计算，不是固定
record-size 估算。为核对全量身份，每个阶段会重读此前所有阶段；该 I/O 在原生
producer 计时窗外，但所需本地日志空间按累计读取量显式检查，总 perf 身份数
限制为 1000000。

## 复现与限制

保存精确 Kafka CLI/JDK 版本、bootstrap、脱敏配置摘要、topic 分区/副本参数、
十进制身份消息数量、acks/compression/batch/linger、限速、producer 数、每命令 UTC
开始时间、wall ms、退出码和原生日志。对比实验必须另固定 broker 拓扑、ISR、
客户端位置、CPU/内存/磁盘/网络和其他写入者；本脚本不把不同条件的数字合并。

权威实现：[Apache Kafka ProducerPerformance 4.4.0](https://github.com/apache/kafka/blob/4.4.0/tools/src/main/java/org/apache/kafka/tools/ProducerPerformance.java)，
尤其 `printTotal()` 的 `1000.0` 与 `1024.0 * 1024.0` 计算及成功回调计数。
