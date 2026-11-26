---
name: kptools-doris-benchmark
description: 当用户要求对已有 Doris 实例实际运行有界、只读 SELECT 合成查询测量，采集多轮客户端往返时延、吞吐和精确结果计数时使用。独立重检实时 FE/BE 与 numbers 能力，分离预热与至少三轮计时，保存原始 JSONL/失败证据；不宣称 TPC-H，不写数据库。
metadata:
  compatibility: Linux；Python 3.9+；用户已有 Doris 2.1.x、3.0.x 或 3.1.x 与 mysql/mariadb CLI
  skill_type: tool_assisted
  category: benchmark
  domain: doris
  version: 1.0.0
---

# Doris 有界 SELECT-only 合成基准

## 范围

对一个**已存在且获授权查询**的 Doris FE 真正启动官方 MySQL/MariaDB CLI，
用 `numbers` 表函数生成 N 行并计算：

```sql
SELECT SUM(number), COUNT(*) FROM numbers("number"="N")
```

默认 N=1,000,000；每次必须精确返回一行 TSV，SUM 为 `N*(N-1)/2`，COUNT
为 N。工作负载实际经过 Doris 执行引擎，但仍是合成只读测量，**不是 TPC-H，
也不代表业务 SQL、导入、用户表扫描或生产容量**。没有建库建表、DML、topic、
Ray 或其他服务操作，也没有任意 SQL 参数。

每次运行先重新检查客户端、认证、当前 FE 版本/状态与可用 BE，再执行
`numbers("number"="1000")` 能力探针并要求精确返回 `499500<TAB>1000`。
脚本不读取或信任旧环境报告，也不依赖 `kptools-doris-environment-check`。
Doris 软件版本取自 `SHOW FRONTENDS` 的 `Version`，不混用 MySQL 协议版本。

## 独立运行与默认负载

在本 skill 根目录运行，无第三方 Python 包或外部源码：

```bash
python3 scripts/run_benchmark.py \
  --host 127.0.0.1 --port 9030 --user root \
  --output results/select-smoke-001
```

默认先执行 1 次独立预热，再执行 **3 个计时轮次，每轮 5 次串行查询**。
可用 `--rows 1..100000000`（默认 1000000）、`--warmup 1..20`、
`--rounds 3..20`、`--queries-per-round 1..100` 和 `--timeout 0.1..300`
调整。轮数不能低于 3，没有无限时长/无限迭代选项，最坏总查询数固定为
2020。每个 CLI 都在新进程组中运行，超时只杀该进程组。

`--host`、`--port`、`--user`、`--client` 支持非默认目标。`--config` 指向
当前用户所有、非链接且无 group/other 权限的 MySQL defaults 文件；也可注入
`DORIS_PASSWORD` 或 `MYSQL_PWD`。密码只进入客户端子进程环境或运行期独占临时
认证文件，绝不进入 argv、日志或报告，临时文件在结束后清理；配置和环境密码不能
同时使用。连接目标 `host`、`port`、`user` 仅由命令行指定；请从配置中移除这些项。
配置可包含 `password`、`ssl-ca`、`ssl-cert`、`ssl-key` 等认证/TLS 白名单选项；
初始化 SQL、include 和其他选项均失败关闭。
客户端使用隔离 HOME 和登录文件路径，不读取隐式 `.mylogin.cnf`；报告只保存脱敏、
截断的证据副本，完整 SQL 输出仅用于内存解析。无密码实例可都不提供。

## 产物和判定

`--output` 必须是新目录，父目录按需创建；任何已有目录都不清空、不覆盖。
成功或已捕获失败均写：

- `result.json`：实时 preflight、精确版本、工作负载、逐轮/汇总指标、限制和错误。
- `queries.jsonl`：预热和计时原始样本（已进入工作负载后），含 UTC 开始时间、
  单调时钟纳秒耗时、阶段、轮次、返回码、脱敏输出和逐项正确性。

预热永不进入计时指标。每轮保留 client-observed round-trip latency（ms）、
queries/s、查询数和 Doris 原生返回值验证计数。时延明确包含新 CLI 进程启动、
TCP 连接、服务端执行等待、结果输出和客户端退出；**不是 Doris server query
latency 或 server profile**。指标公式和官方资料见
[references/metrics.md](references/metrics.md)。

- 退出 `0` 且 `status=passed`：所有实时前置、预热和请求轮次均完成且精确正确。
- 退出 `1`：`blocked` 表示实时 preflight 失败；`failed` 表示工作负载超时、
  认证/运行错误或 correctness mismatch。失败样本和结果保留，不继续后续轮次。
- 退出 `2`：参数或输出文件系统错误；既有证据不删除。

## 可执行契约

```bash
bash cases/contract/run.sh
```

Tier-A 使用遵守列名抑制参数的假 CLI 检查成功、表头形状、numbers 能力、
自定义行数、工具/版本/认证/超时/运行失败、输出冲突、预热排除、精确正确性、
指标数值和脱敏。它不是实际 Doris E2E，不产生真实性能数值；真实结论只能来自
上述命令对指定现场的新测量。
