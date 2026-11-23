# ClickHouse 部署与测试

## 环境与安装

已有实例直接进入连接验证。新部署先确认版本、CPU 架构、内存、磁盘、端口、数据保存位置，以及裸机或容器；从 [官方 Releases](https://github.com/ClickHouse/ClickHouse/releases) 的目标版本 Assets 下载对应架构的 `clickhouse-common-static-<版本>-<架构>.tgz`，不要把 GitHub 页面另存为安装包。Linux x86_64 对应 amd64，aarch64 对应 arm64。解压后真正的可执行文件在包内 `usr/bin/clickhouse`，复制到新实验目录并赋予执行权限。下载后记录版本与 SHA-256，不使用 latest 作为可复现版本标识。需要源码构建时使用目标 release tag 及其构建文档，不替换系统编译器。

示例命令使用 Linux/Bash。依赖 Python 3.10+、tar、可用磁盘与对应架构的 ClickHouse；TPC-H 的源码获取另需 Git，生成数据另需 C 编译器和 Make。

单二进制本地测试：将下载的 clickhouse 可执行文件放到新建实验目录，在该目录执行 `./clickhouse server`；另开终端执行 `./clickhouse client --query 'SELECT version(), 1'`。数据留在实验目录。已有配置用 `--config-file` 显式指定。部署验证应包括进程日志、实际连接地址和 SQL 响应。

以下示例假设 clickhouse-client 与 clickhouse-benchmark 已在 PATH。单二进制安装分别改为 `./clickhouse client` 与 `./clickhouse benchmark`；远程连接在所有命令中保持相同 host、port、user 和 TLS 参数，认证使用客户端配置文件。

## 无数据依赖的最小验证

```bash
clickhouse-client --host localhost --port 9000 --query 'SELECT 1'
clickhouse-client --host localhost --port 9000 --query 'SELECT sum(number) FROM numbers(1000)'
```

两条结果应分别为 `1`、`499500`。随 skill 提供的 `scripts/check_service.py` 会自动检查这些结果并保存原始输出，单二进制情况下，在 skill 根目录运行 `python3 scripts/check_service.py --client experiment/clickhouse --single-binary --output results/smoke-001`，其中 experiment/clickhouse 是你复制的二进制位置。已安装 clickhouse-client 时使用 SKILL.md 中的默认命令。

该脚本只验证连接、版本响应和合成 SQL 结果，不检测远端节点或副本状态。集群场景须另行核对配置节点清单，并在各节点检查 [system.replicas](https://clickhouse.com/docs/operations/system-tables/replicas) 的 is_readonly、is_session_expired、active_replicas/total_replicas 和 absolute_delay；延迟阈值按业务要求确定。无复制表时该表为空，不能据此认定整个集群健康。

## 可直接执行的有限查询基准

使用 [官方 clickhouse-benchmark](https://clickhouse.com/docs/operations/utilities/clickhouse-benchmark)。以下是合成计算基准，不代表磁盘扫描或业务 SQL。`bash scripts/run_benchmark_rounds.sh` 为每次实验新建目录：目录已存在时以退出码 3 停止且不触碰旧文件；预热写入 `warmup.log`，每个测量轮次写入独立的 `round-<n>.log`，任一轮失败即停止。始终用 `bash` 显式调用，因此复制或安装后权限为 0644 也可用：

```bash
bash scripts/run_benchmark_rounds.sh --output "results/run-$(date -u +%Y%m%dT%H%M%SZ)" --rounds 3 -- \
  --host localhost --port 9000 --concurrency 1 --iterations 10 \
  --query 'SELECT sum(number) FROM numbers(1000000)'
```

单二进制安装使用 `--client ./clickhouse --subcommand benchmark`。脚本只接受新目录；需要保留的上一轮输出不会被同名重定向截断。统计各轮日志中的 QPS/延迟并保留全部轮次值；固定并发、查询、版本与线程设置。改测真实 SQL 时将已验证查询传给 `--query`，多条查询按官方工具的输入格式保存成文件并通过标准输入传入。始终设置有限 iterations。

## TPC-H

使用 [官方 TPC-H 指南](https://clickhouse.com/docs/get-started/sample-datasets/tpch) 链接的公开数据生成器、ClickHouse 建表 SQL 与查询集，记录各仓库 commit；建表文件是 ClickHouse 仓库的 `tests/benchmarks/tpc-h/init.sql`，查询文件在同目录 `queries/`，不是网页正文。先用以下命令取得它们：

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/ClickHouse/ClickHouse.git clickhouse-source
git -C clickhouse-source sparse-checkout set tests/benchmarks/tpc-h
git -C clickhouse-source rev-parse HEAD
```

上游 `init.sql` 使用 `CREATE TABLE nation` 等未限定库名的语句，文件中没有可直接替换的库名；仅传 `--multiquery` 时表会建在当前连接默认库。先确认一个已授权的实验库（不存在时由有权限的账号先创建），在 DDL、导入和查询三处显式传入相同的 `--database`：

```bash
set -euo pipefail
CH=(clickhouse-client --host localhost --port 9000)
export DB=tpch_sf1  # 替换为已授权的实验库
"${CH[@]}" --database "${DB}" --multiquery < clickhouse-source/tests/benchmarks/tpc-h/init.sql
# dbgen 的 .tbl 以 | 分隔且行尾多一个 |；去掉行尾分隔符并转为制表符分隔
for table in region nation supplier part partsupp customer orders lineitem; do
  test -s "$table.tbl" || { echo "缺少生成数据：$table.tbl" >&2; exit 1; }
  sed 's/|$//; s/|/\t/g' "$table.tbl" |
    "${CH[@]}" --database "${DB}" --query "INSERT INTO $table FORMAT TSV"
  expected=$(wc -l < "$table.tbl")
  actual=$("${CH[@]}" --database "${DB}" --query "SELECT COUNT(*) FROM $table")
  test "$actual" -eq "$expected" || { echo "$table 行数不符" >&2; exit 1; }
done
# 八表导入及行数核对全部完成后，才校验查询结果并进入计时阶段
"${CH[@]}" --database "${DB}" --query "$(cat clickhouse-source/tests/benchmarks/tpc-h/queries/query_01.sql)"
```

八张表的 `.tbl` 都按相同方式去掉行尾 `|` 并转为制表符分隔，再以 `FORMAT TSV` 导入；`query_01.sql` 至 `query_22.sql` 逐条查询时也使用同一个 `--database`。`init.sql` 中没有可替换的库名，不要依赖不存在的库名替换；若确需在已授权的实验范围内做 DDL 适配（例如引擎、分区或索引调整），应审查改动并保留记录，但不能因此省略 `--database`。否则表或查询会落到默认库。读取同目录 README.md 处理查询与规模适配；主分支文件并不自动适用于所有服务版本。先从 SF=1 验证，再按容量选择规模。执行前检查无覆盖已有表的动作。

顺序：生成八张表数据 → 执行已审查 DDL → 按表导入 → 比对源文件记录数与 COUNT(*) → 校验查询结果 → 预热与多轮查询。保存生成参数、DDL/查询/数据清单校验和及导入日志。查询性能与合成计算基准分开报告。

下载与运行是两个独立检查：下载应使用 `curl -fL` 或等价的 HTTP 错误检查，解压前执行 `tar -tzf` 检查文件内容；不能将 HTML 登录页当成二进制。若目标主机不能联网，可在可联网机器下载同版本同架构安装包和测试源码，再校验后传入目标机，命令保持不变。认证、权限和网络连通性需在部署前验证。
