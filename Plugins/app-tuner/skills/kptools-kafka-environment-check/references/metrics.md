# 负载、输出字段与指标边界

本 skill 测量的是“能否在明确范围内开始一次合成 producer 压测”，不生成性能
数字，不把 metadata 延迟当生产延迟，也不声称 broker 生产健康。

## 计划字段

`workload` 记录 `rounds`、`records_per_round`、`warmup_records`、十进制
monotonic `payload` 与 `throughput_records_per_second`。预热不计入计时轮。
`workload_budget.evidence` 另给出：

- `planned_payload_bytes`：各阶段 `sum(len(str(i)))` 的总和，身份在每阶段从 0
  开始；不包含三条正确性消息、broker 副本、索引、协议开销和 retention 余量。
- `planned_perf_identities`：预热与计时轮消息总数，上限 1000000。
- `cumulative_verification_payload_bytes`：每阶段精确核对当前全部历史消息时
  需要重读的 payload 总量；`output_storage` 据此加 16 MiB 余量检查结果盘。
- `estimated_throttled_seconds_per_round`：`records / throughput`，只是用户
  限速下的理想估计，不是实测值或吞吐上限。
- `available_bytes`：仅本机结果目录所在文件系统的可用字节，不是 broker 磁盘。

参数拒绝无限/零/负数速率、非有限超时、少于 3 轮和无界消息数。限速参数没有
`-1` 无限速入口；如需逼近容量，应由资源所有者另行设定更高但仍有限的计划。

## JSON 语义

每个探针有独立的 `status=ready|blocked|unknown`、`category`、`mandatory` 和
`evidence`。顶层同名状态是强制探针的保守汇总。`diagnostic` 是截断且脱敏的
原生错误尾部；topic 名列表不收集。配置只保存 SHA-256、权限、键名和
`property_values=redacted`，不保存属性值。

检查通过后，实际压测仍须重新执行实时前置检查；不能将旧 `result.json` 当长期
通行证。实际原生 records/s、MiB/s、latency、offset 和 wall timing 的定义属于
压测执行结果，本检查不伪造这些字段。

官方术语与配置依据：[Apache Kafka 4.1 Configuration](https://kafka.apache.org/41/configuration/)。
