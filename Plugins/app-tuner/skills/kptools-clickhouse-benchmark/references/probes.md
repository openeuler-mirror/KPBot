# 基准预检与进程协议

## 每次运行都重新执行

1. 检查新输出目录及至少 1 MiB 日志空间。
2. 独立客户端读取版本；单二进制用全局 `--version`。不执行 `benchmark --version`。
3. 执行 benchmark `--help`，验证 `--query`、`--iterations`、`--concurrency`、`--delay`、`--client-side-time`，以及配置模式需要的 `--config`。
4. 对当前目标执行 `SELECT version()`，验证认证、目标与版本下限 22.8.0。
5. 精确执行 `SELECT sum(number) FROM numbers(1000)` 并要求 `499500`。
6. 运行一次独立预热，再运行默认三轮测量。预热和每轮后都重新执行同一工作负载 SELECT，并精确核对 `N*(N-1)/2`。

任一阶段失败都会 fail closed：不将旧报告当作实时证据，不把后续轮次标为成功，不计算成功汇总。已完成阶段和非零返回码原样保留。

退出码遵循系列契约：`0=成功`，`1=预检或负载失败`，`2=参数或输出错误/冲突`，`3=unknown（保留；当前无总体 unknown 状态）`，`130=中断`。

## 有界执行与清理

- 所有轮次、迭代、行数、并发和子进程时间均有 CLI 上限；不转发 `--timelimit=0` 或 `--iterations=0`。
- 每个子进程由 `Popen(start_new_session=True)` 创建独立进程组。超时或取消（包括 `KeyboardInterrupt`）立即只对该进程组发 SIGKILL，即使组长已退出也能清除忽略 SIGTERM 并继承输出管道的后代；回收与管道读取均有有限超时，取消异常在清理后原样抛出，不执行全局服务停止命令。
- 测量工具 stdin 为 `DEVNULL`，固定 SQL 由 `--query` argv 传入，不经过 shell 插值，也不支持调用方追加 SQL。
- `--delay 0` 禁止中间报告，解析器要求最终报告的查询计数与请求的固定迭代数完全一致。
- 工作负载进程失败或超时后不再发额外验证查询，避免继续增加目标负载。
- 只写新输出目录；不清理已有输出，不删除数据库对象或临时表，因为协议根本不会创建它们。

## 版本证据边界

三段和四段版本均完整保留。单二进制的客户端与 benchmark 版本来自同一个全局版本输出。独立 benchmark 没有可移植的版本命令，因此记录 `unavailable` 和实际 `--help` 证据，而不是从服务端或客户端编造相同版本。

## 官方依据

- [官方 clickhouse-benchmark 参数与输出](https://clickhouse.com/docs/operations/utilities/clickhouse-benchmark)
- [官方客户端接口](https://clickhouse.com/docs/interfaces/cli)
- [有界 `numbers(N)` 表函数](https://clickhouse.com/docs/sql-reference/table-functions/numbers)
- [`sum` 聚合函数](https://clickhouse.com/docs/sql-reference/aggregate-functions/reference/sum)
