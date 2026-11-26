# 指标、单位与复算口径

## 范围

唯一负载是合成 `SELECT sum(number) FROM numbers(N)`，只测该 SQL 在当前客户端、网络、服务端与参数下的表现。它不是 TPC-H、磁盘扫描、导入、写入、复制或业务混合负载，也不提供生产容量结论。

## 每轮原生字段

| JSON 字段 | 单位 | 来源与含义 |
| --- | --- | --- |
| `native.query_count` | 查询次数 | 官方最终报告的 `queries`；还必须等于 `Queries executed` 和本轮固定 `--iterations`。 |
| `native.qps` | queries/s | 官方 QPS，当前报告窗口为本次最终单端点运行。 |
| `native.read_rows_per_second` | rows/s | 官方 RPS，服务端报告读取行数的速率。 |
| `native.read_mib_per_second` | MiB/s | 官方读取字节速率，1 MiB = 1,048,576 字节。 |
| `native.result_rows_per_second` | result rows/s | 官方结果行速率。 |
| `native.result_mib_per_second` | result MiB/s | 官方结果字节速率。 |
| `native.latency_ms.p50/p95/p99` | 毫秒 | 官方秒值乘 1000；因显式 `--client-side-time`，是客户端侧查询执行时间。 |
| `wall_time_ms` | 毫秒 | Python `time.monotonic_ns()` 测得整个 benchmark 子进程墙钟时间，包含进程启动和收尾，不等同原生查询分位数。 |
| `correctness.duration_ms` | 毫秒 | 计时之外的精确 SELECT 验证子进程耗时，不并入原生 QPS。 |

解析器接受官方输出中的 `50%`/`50.000%` 等等价显示，但不插值、不补造缺失分位数。ClickHouse 26.8 的 `queries: 3`、`Queries executed: 3 (100%)` 和整数百分比文本有精确回归样本。

## 汇总

只有全部测量轮次通过才生成 `summary`：

- `median_qps`、`min_qps`、`max_qps` 只取 `rounds[]` 的原生 QPS。
- `median_wall_time_ms` 只取测量轮次墙钟时间。
- `median_native_latency_ms` 分别对测量轮次 p50、p95、p99 取中位数。
- `total_native_queries` 是测量轮次原生查询计数之和，不含预热和正确性查询。
- `warmup.included_in_summary=false`；预热即使 QPS 很高也不得进入任何汇总。

原始日志是复算依据。JSON 的计数、速率和延迟必须能从相应 `round-N.stdout.log`/`stderr.log` 找到；仅有 result.json 文件或进程返回零不能替代语义验收。任何指标格式未知、计数不符、运行失败或正确性失败均无成功汇总。

官方定义见 [clickhouse-benchmark Output](https://clickhouse.com/docs/operations/utilities/clickhouse-benchmark#output)。
