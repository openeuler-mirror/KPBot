# 官方 LDBC SNB Interactive v2 Driver 验证

> 这是与 `sandbox/run_benchmarks.py` 不同的第二条验证路径：官方 Java driver + PostgreSQL
> 实现，新增 `pggraph` 模块，用 `graph.traverse`/`shortest_path`/`gql` 执行 LDBC 查询，
> 并与 postgres 实现对比。源资料提供 `ldbc_pggraph_impls.patch` 和
> `ldbc_pggraph_driver.patch`。

## 1. 适用场景

- 需要 LDBC 官方审计指标（Throughput、Schedule Audit、Excessive Delay、per-query
  latency），而不只是自建 hot/throughput。
- 需要与原生 PostgreSQL 实现做同 workload 对比。
- 需要验证 Interactive v2 的 14 条复杂读、7 条短读、8 插入 + 8 删除更新流。

不适用：只关心 pgGraph 单查询延迟/内核热点；那用 `sandbox` 基准或 hot/throughput 更快。

## 2. 组成

| 补丁/目录 | 作用 |
|-----------|------|
| `ldbc_pggraph_impls.patch` | 在 `ldbc_snb_interactive_v2_impls` 新增 `pggraph` 模块：pom、`PgGraphDb`/`PgGraphQueryStore`/`PgGraphInteractiveDb`、converter、queries、ddl、load 脚本、driver 配置 |
| `ldbc_pggraph_driver.patch` | 修复 DuckDB JDBC 兼容、日期解析、4-hop 路径列数等 |
| `pggraph/queries/interactive-complex-*.sql` | 14 条复杂读，使用 `graph.traverse`/`shortest_path` |
| `pggraph/queries/interactive-short-*.sql` | 7 条短读（SQL，与 postgres 相同） |
| `pggraph/queries/interactive-update-*.sql` | 8 插入 + 8 删除 |
| `pggraph/ddl/pggraph-register.sql` | 表/边注册 |
| `pggraph/scripts/load*.sh` | 本地加载 + 注册 + build |
| `pggraph/driver/benchmark.*` | 驱动配置/启动 |

## 3. 前置条件

- PostgreSQL + pgGraph 已安装（端口/用户/库按实际）。
- Java 17、Maven 3.6+。
- Python 3.10+：`duckdb`、`pyarrow`、`networkit`、`psycopg`。
- LDBC 数据（SF0.003 示例或自生成 SF1+）。注意**两套 LDBC 数据互不兼容**：
  build-deployment 导入片用 v1 `CsvBasic`（解压后归一化入 `postgres` 库），本片
  driver 验证用 v2 `composite-merged-fk`（`graphs/csv/bi/…` 目录形态，须**另建库**
  `ldbcsnb`，不能复用 v1 导入的库/表）——schema 与文件布局不同，混用会加载失败。
- 两个上游仓库的源码树：`${LDBC_IMPLS_DIR}`、`${LDBC_DRIVER_DIR}`。

## 4. 步骤

### 4.1 应用补丁

```bash
cd "${LDBC_IMPLS_DIR}"
git apply --check /path/to/ldbc_pggraph_impls.patch
git apply /path/to/ldbc_pggraph_impls.patch

cd "${LDBC_DRIVER_DIR}"
git apply --check /path/to/ldbc_pggraph_driver.patch
git apply /path/to/ldbc_pggraph_driver.patch
```

先 `--check`，失败不要强推；补丁基于特定上游版本。

### 4.2 构建

```bash
cd "${LDBC_DRIVER_DIR}"
export JAVA_HOME=/path/to/java17
mvn clean package -DskipTests

cd "${LDBC_IMPLS_DIR}"
mvn clean package -DskipTests
```

### 4.3 准备参数

```bash
cd "${LDBC_DRIVER_DIR}/paramgen"
# 上游 paramgen/ 下无二层 paramgen/ 目录：脚本相对 paramgen 根引用（./scripts/…，
# ./ 前缀显式相对 cwd——外部仓库路径不带本仓库 skill 的 scripts/ 链接语义），
# paramgen.py 内部又以相对路径访问 schema.sql 等，故 cd 后不能再带 paramgen/ 前缀
bash ./scripts/get-sample-data.sh
. ./scripts/use-sample-data.sh

python3 paramgen.py \
  --raw_parquet_dir "${LDBC_SNB_DATA_ROOT}/graphs/parquet/raw/" \
  --factor_tables_dir "${LDBC_SNB_FACTOR_TABLES_DIR}" \
  --time_bucket_size_in_days 1 \
  --generate_short_query_parameters True \
  --threshold_values_path 'paramgen_window_values_test.json'
```

源资料还要求清理参数 parquet 中的 pandas 索引列（`level_N`）；否则 driver 可能读到
多余列。清理脚本要按实际列名处理，不要硬编码表名。

### 4.4 转换更新流

Interactive v2 的 insert/delete 流需要转成 parquet；源资料给出 DuckDB 脚本：
按实体合并 CSV、日期列转 epoch millis、输出 parquet。转换后检查每个实体文件存在、
时间列类型一致。

### 4.5 加载 + 注册 + 建图

```bash
cd "${LDBC_IMPLS_DIR}/pggraph"
export PGGRAPH_HOST="${PGHOST}"
export PGGRAPH_PORT="${PGPORT}"
export PGGRAPH_USER="${PGUSER}"
export PGGRAPH_PASSWORD=""          # 用 .pgpass/peer，不要写明文
export PGGRAPH_DATABASE="ldbcsnb"
export PGGRAPH_CSV_DIR="${LDBC_SNB_DATA_ROOT}/graphs/csv/bi/composite-merged-fk/"
# ↑ v2 composite-merged-fk 数据（与 build-deployment 片的 v1 CsvBasic 导入互不
#   兼容——两套各自建库，本片用下方新建的 ldbcsnb，不复用导入片的 postgres 库）
export PSQL_BIN="${PGSQL_PREFIX}/bin/psql"

psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d postgres \
  -c "CREATE DATABASE ldbcsnb;"
psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d ldbcsnb \
  -c "CREATE EXTENSION IF NOT EXISTS graph;"

bash ./scripts/load-in-one-step.sh   # 当前目录已是 ${LDBC_IMPLS_DIR}/pggraph，不要再加一层 pggraph/；或用 ${LDBC_IMPLS_DIR}/pggraph/scripts/load-in-one-step.sh 全路径
```

### 4.6 运行

```bash
cd "${LDBC_IMPLS_DIR}/pggraph"
export JAVA_HOME=/path/to/java17
CLASSPATH="${LDBC_DRIVER_DIR}/target/driver-standalone.jar"
CLASSPATH="${CLASSPATH}:${LDBC_DRIVER_DIR}/target/classes"
CLASSPATH="${CLASSPATH}:target/pggraph-2.0.0-SNAPSHOT.jar"
CLASSPATH="${CLASSPATH}:../common/target/common-2.0.0-SNAPSHOT.jar"
CLASSPATH="${CLASSPATH}:../postgres/target/postgres-2.0.0-SNAPSHOT.jar"

java -cp "${CLASSPATH}" org.ldbcouncil.snb.driver.Client \
  -P driver/benchmark.properties \
  -P "${LDBC_DRIVER_DIR}/target/classes/configuration/ldbc_driver_default.properties"
```

## 5. 查询适配

| 查询 | 适配 |
|------|------|
| Q1 传递好友 1–3 跳 | `graph.traverse(depth=3, edge_types=['knows'])` |
| Q2 好友最近消息 | traverse(1) + SQL |
| Q3 好友的好友去过国家 | traverse(2) + SQL |
| Q4 好友新话题 | traverse(1) + SQL |
| Q5 新群组 | traverse(2) + SQL |
| Q6 标签共现 | traverse(2) + SQL |
| Q7 最近点赞者 | SQL |
| Q8 最近回复 | SQL |
| Q9 好友最近消息 | traverse(2) + SQL |
| Q10 好友推荐 | traverse(2, filter depth=2) + SQL |
| Q11 工作推荐 | traverse(2) + SQL |
| Q12 专家搜索 | traverse(1) + SQL |
| Q13 最短路径 | `graph.shortest_path()` |
| Q14 加权路径 | SQL 递归 CTE（同 postgres） |

短读/更新与 postgres 实现相同；pgGraph 的价值在复杂读的遍历部分，不在所有查询。

## 6. 指标与结果

| 指标 | 含义 |
|------|------|
| Throughput (ops/sec) | 每秒完成操作数 |
| Schedule Audit | 95% 操作是否在计划时间 1s 内启动；PASSED |
| Excessive Delay Count | >1000ms 的操作数 |
| Per-query latency | min/mean/p50/p95/p99 |

结果文件：`results/LDBC-SNB-pgGraph-results.json`、`validation.json`、
`results_log.csv`。

源资料 SF0.003 示例：Throughput 约 106.67 ops/sec，Schedule Audit PASSED，
Excessive Delays 0；这不是 SF1 的通用指标。

## 7. 对比 postgres 实现

用同一参数/数据/驱动，把实现类换成 `PostgresInteractiveDb`，运行 postgres 模块；
比较 Throughput/延迟/审计。注意：

- 两侧数据加载、注册、图构建成本不同；不要只比查询延迟。
- pgGraph 的复杂读可能走 traverse+SQL，SQL 部分仍受 PG 计划/索引影响。
- 更新流会触发 pgGraph 同步/覆盖层，写路径开销要与 postgres 分开比较。

## 8. 故障排查

| 现象 | 处理 |
|------|------|
| `Timestamp format must be yyyy-mm-dd` | 应用 driver 补丁的日期兼容 |
| `column 'firstName' does not exist` | PG 折叠小写；注册脚本用小写列名 |
| `graph extension not found` | `CREATE EXTENSION graph` |
| `No CSV-files found` | 检查 `PGGRAPH_CSV_DIR` 指向 composite-merged-fk |
| DuckDB 错误 | 检查 DuckDB Python/JDBC 版本；driver 补丁已升级 JDBC |
| 4-hop 路径列数不匹配 | 应用 driver 补丁的 path_selection 修复 |
| 参数 parquet 多出列 | 清理 pandas 索引列 |

## 9. 与 sandbox 基准的关系

| 路径 | 优点 | 局限 |
|------|------|------|
| sandbox hot/throughput | 快速、单查询细粒度、可 checksum | 非官方审计、查询集固定 12 条 |
| 官方 driver | 标准 workload/审计/更新流 | 依赖 Java/Maven/参数生成，准备重 |

两者都保留：sandbox 用于迭代和热点，driver 用于最终合规/对比。不要把两者的
Throughput 直接横比（workload/并发/审计口径不同）。
