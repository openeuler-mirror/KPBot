# 探针与实时判定

## 探针清单

| 探针 | 必需性 | 实际证据与就绪条件 |
| --- | --- | --- |
| `output_storage` | 必需 | 新证据目录可创建；所在文件系统至少有 1 MiB 可用空间用于日志。不是服务端磁盘容量检查。 |
| `client_tool` | 必需 | 独立客户端执行 `clickhouse-client --version`；单二进制执行 `clickhouse --version`。输出须包含完整三段或四段数字版本。 |
| `benchmark_interface` | 必需 | 执行 benchmark `--help`，必须出现 `--query`、`--iterations`、`--concurrency`、`--delay`、`--client-side-time`；使用配置时还需 `--config`。缺工具、非零退出或缺选项不能就绪。 |
| `server_interface` | 必需 | 通过实际目标、用户、TLS/配置执行 `SELECT version()`，证明连接与认证可完成该 SELECT；解析版本不得低于 22.8.0。 |
| `exact_query` | 必需 | `SELECT sum(number) FROM numbers(1000)` 的原始唯一输出必须精确等于 `499500`，零退出但结果错误仍为 blocked；只有写入日志和 JSON 的证据副本会脱敏。 |
| `client_server_series` | 可选 | 比较客户端和服务端 major.minor；不一致记录 unknown，但由上述真实接口检查决定总体就绪，不靠猜测拦截。 |

`benchmark --version` 不是受支持的可移植接口，两个布局均不调用。单二进制的 benchmark 版本复用同一二进制的全局版本；独立 benchmark 只陈述实际验证过的帮助选项。

## 状态和错误分类

- 每个探针均为 `ready`、`blocked` 或 `unknown`，并带 `mandatory` 和 `reason`。
- 任一必需探针 `blocked` → 总体 `blocked`。
- 没有 blocked、但至少一个必需探针因版本格式或前置依赖而为 `unknown` → 总体 `unknown`。
- 所有必需探针 ready → 总体 `ready`。可选系列提示不会单独阻断真实接口已经验证的目标。
- 子进程 `FileNotFoundError` → `missing_tool`；有限超时 → `timeout`；认证错误标记 → `auth`；低于下限或缺接口 → `wrong_version`；其他非零退出 → `error`。
- 退出码遵循系列契约：`0=ready`，`1=blocked`，`2=参数或输出错误/冲突`，`3=unknown`。

不会用 socket-open、Python 包可导入或版本字符串本身代替上述检查。远程账号只需要执行这两个 SELECT 的权限；脚本不读取集群表，也不据此推断集群健康。

## 官方依据

- [ClickHouse benchmark：参数、有限迭代和输出](https://clickhouse.com/docs/operations/utilities/clickhouse-benchmark)
- [ClickHouse 客户端接口](https://clickhouse.com/docs/interfaces/cli)
- [`numbers(N)`：生成 0 到 N-1 的有界序列](https://clickhouse.com/docs/sql-reference/table-functions/numbers)
- [`version()` 等其他函数](https://clickhouse.com/docs/sql-reference/functions/other-functions)
- [官方原生协议与安全连接说明](https://clickhouse.com/docs/interfaces/tcp)
