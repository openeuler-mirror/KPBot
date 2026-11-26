# 指标、复算与解释边界

## numbers 工作负载和正确性

负载为 `SELECT SUM(number), COUNT(*) FROM numbers("number"="N")`。N 是仅限
整数的 `--rows` 参数，默认 1,000,000，最大 100,000,000；不存在 SQL 文本插值
入口。每个查询应返回一行，Doris 原生结果为：

```text
SUM(number) = N * (N - 1) / 2
COUNT(*)    = N
```

预热前先用 N=1000 做实时能力探针，期望精确 TSV `499500<TAB>1000`。所有实际
查询按各自 N 计算期望值，比较完整单行 TSV；多行、缺列、错误数值、非零返回码、
认证错误和超时都不是“零耗时样本”，而是保留证据并 fail closed。

该负载生成数字并执行聚合，不读取用户表、不清缓存、不导入数据，也不是 TPC-H。
N 改变时只可与明确相同 N 的结果直接比较；不同服务、客户端位置或配置不能
直接比较。

## 时间与轮次

- 单查询 `elapsed_ns`：在父进程启动 CLI 前后读取 `time.monotonic_ns()`，原值
  写入 JSONL；展示时延以 `elapsed_ns / 1,000,000` 转为 **ms**。
- 单查询范围是 **client-observed round-trip**：新 CLI 启动、TCP 建连、等待
  服务端执行、结果输出和客户端退出。它不是 Doris server query latency。
- `round_wall_elapsed_ns`：一个计时轮次全部串行查询的父进程单调墙钟区间。
- 预热使用相同查询和正确性检查，但独立记录，永不进入轮次或汇总。
- 默认 1 次预热 + 3 轮 × 5 查询；最少 3 个计时轮次。

每轮和成功汇总中的 `query_latency` 对相应计时样本给出 count、min、mean、
p50、p95、max。p50/p95 使用排序后的 nearest-rank：`ceil(p*N)` 个元素（从 1
开始）。原始纳秒样本是复算真源，JSON 浮点毫秒只是展示值。

## 吞吐和原生计数

每轮吞吐：

```text
queries_per_second = 本计时轮完成的精确正确查询数
                     / (round_wall_elapsed_ns / 1,000,000,000)
```

汇总吞吐使用所有完整计时轮查询数除以这些轮的 wall time 之和。当前串行
`concurrency=1`，没有把多个客户端重叠时间错误相加。失败运行不发布成功汇总。

`validated_native_results` 记录每查询生成 N 行、返回一行、SUM=N*(N-1)/2、
COUNT=N 以及精确匹配数；这些是 Doris 实际返回并验证的原生结果。时延和吞吐
则是客户端观测，不是 FE/BE server profiling、集群总吞吐或业务 QPS。

## CLI 列名和失败证据

标量 SELECT 使用 `--skip-column-names`，使精确 TSV 不含表头；`SHOW FRONTENDS`
和 `SHOW BACKENDS` 必须保留列名供按列解析。回归假 CLI 遵守同一开关，避免把
永远打印表头的假输出误当作真实 CLI 形状。Doris 软件版本读取 FE `Version`，
不使用会返回 MySQL 协议版本的 `SELECT version()`。

实时 preflight 失败只写 `result.json`，其中保留各 SQL 的返回码、耗时、超时和
脱敏输出。工作负载开始后的失败另有 `queries.jsonl`，已完成样本不删除，后续
轮次不执行。输出目录独占创建，脚本不会删除旧目录来“重试成功”。

## 官方资料

- [Doris 3.x：SELECT](https://doris.apache.org/docs/3.x/sql-manual/sql-statements/data-query/SELECT/)
- [Doris 3.x：numbers 表函数](https://doris.apache.org/docs/3.x/sql-manual/sql-functions/table-valued-functions/numbers/)
- [Doris 3.x：SHOW FRONTENDS](https://doris.apache.org/docs/3.x/sql-manual/sql-statements/cluster-management/instance-management/SHOW-FRONTENDS/)
- [Doris 3.x：SHOW BACKENDS](https://doris.apache.org/docs/3.x/sql-manual/sql-statements/cluster-management/instance-management/SHOW-BACKENDS/)
- [Doris 3.x：官方 TPC-H 流程](https://doris.apache.org/docs/3.x/benchmark/tpch/) — 本合成 numbers 查询不满足该流程，禁止冒称 TPC-H。
- [MySQL 8.0：client command options](https://dev.mysql.com/doc/refman/8.0/en/mysql-command-options.html)
- [Python：time.monotonic_ns](https://docs.python.org/3/library/time.html#time.monotonic_ns)

实现中的 TCP-only argv 和精确标量检查参考了[已有 Doris 部署技能](https://gitcode.com/openeuler/KPBot/merge_requests/205)中的 `check_service.py`；
此 skill 的实时 preflight、轮次、指标、原始证据和进程清理由本目录独立实现。
