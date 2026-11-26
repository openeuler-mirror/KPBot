---
name: pggraph-build-deployment
description: >
  pgGraph 0.1.8（PostgreSQL 14–18 的 Rust/pgrx 图扩展）在 aarch64/鲲鹏与 x86_64 Linux 上的
  编译、安装、部署、数据导入与运维：Rust 1.96.0 + cargo-pgrx 0.19.1 + pg_config/服务端头文件，
  `cargo pgrx install`/Makefile、pgNN feature 匹配、graph.so/control/sql 产物、PostgreSQL
  初始化与启动、CREATE EXTENSION graph、本地/容器两种基准入口、LDBC SNB SF1 与 Panama
  数据准备/COPY/注册/建图、工件与日志、备份恢复、clean-graph、版本切换与回滚。用于从零
  搭建、复制部署、扩容、升级或排查部署问题；不适用参数/源码优化
  （→ pggraph-performance-optimization）、压测与正确性验证（→ pggraph-test-verification）、
  热点归因（→ pggraph-hotspot-analysis）。触发词：pgGraph 编译、pgGraph 部署、pgGraph 安装、
  cargo pgrx、graph.so、CREATE EXTENSION graph、LDBC 导入、Panama、graph.build、clean-graph。
license: MulanPSL-2.0
compatibility: pgGraph 0.1.8；PostgreSQL 18.3、Rust 1.96.0、pgrx 0.19.1、aarch64/鲲鹏实测；PG 14–18 与 x86_64 需重新核对 feature/依赖
metadata:
  skill_type: tool_assisted
  category: deployment
  domain: graph_database
  version: "1.0.0"
---

# pgGraph 编译与部署

## 简介

把“装依赖 → 编译扩展 → 启动 PostgreSQL → 建扩展 → 准备数据 → 注册 → 建图 →
验收 → 备份/恢复 → 升级/回滚”固定成可复现流程。所有命令参数化，路径用变量占位。

**实测基线**：PostgreSQL 18.3、Rust 1.96.0、pgrx 0.19.1、pgGraph 0.1.8，
aarch64/鲲鹏环境；LDBC SF1 构建数百秒、CSR 约 830MB（具体以 `report.json` 为准）。
其他机型只改变时间/内存量级，不改变流程。

## 何时触发

- 在 Linux/aarch64 上从零编译、安装 pgGraph，或重新编译验证源码改动。
- 配置 PostgreSQL 实例、端口、数据目录、连接数，创建 `graph` 扩展。
- 准备 LDBC/Panama 数据，执行 COPY、`graph.add_table/add_edge`、`graph.build`。
- 图工件损坏/格式变更，需要 `clean-graph` 重建。
- 备份/恢复、换二进制、升级/回滚、排查启动与构建失败。

**不适用**：线程/缓存/WAL/连接池调优 → `pggraph-performance-optimization`；
压测口径与正确性 → `pggraph-test-verification`；perf/火焰图 → `pggraph-hotspot-analysis`。

> 交接说明：上述姊妹 skill 与本 skill 同属 pggraph-optimize 领域分组，由同一系列 PR 交付
> （源码位于 `Plugins/app-tuner/skills/pggraph-optimize/`，`install.sh` 会把五个成员平铺
> 安装到 `<tool>/skills/<name>/`，安装后入口即可用——该安装行为随系列第 1 个 PR 交付的
> `pggraph-optimize/DOMAIN.md`（install.sh 依赖它识别分组）合入后生效；此前 install.sh
> 对缺 DOMAIN.md 的领域分组按设计整组跳过，已合入成员暂不安装，属预期窗口期）。若当前环境找不到对应入口
> （未安装/未合入），把部署状态、日志与错误信息连同待办交回维护者，由其安装对应
> skill 或人工继续，不要凭空调参/改码。

## 变量约定

执行前先填表，正文与脚本统一引用；示例值仅为占位，不是真实环境。

| 变量 | 含义 | 示例 |
|------|------|------|
| `${PGGraph_SRC}` | pgGraph 工作源码树 | `/opt/pggraph/pgGraph-0.1.8` |
| `${PGGraph_SRC_BAK}` | 原始只读备份树 | `/opt/pggraph/pgGraph-0.1.8-bak` |
| `${PGGraph_HOME}` | 基准脚本/结果目录 | `/opt/pggraph/bench` |
| `${PGSQL_PREFIX}` | PostgreSQL 安装前缀 | `/opt/pgsql/18.3` |
| `${PGDATA}` | PostgreSQL 数据目录 | `/data/pg18` |
| `${PGPORT}` / `${PGHOST}` | 连接端口/主机 | `6666`、`localhost` |
| `${PGUSER}` / `${PGDB}` | 用户/数据库 | `postgres`、`postgres` |
| `${LDBC_ARCHIVE}` | LDBC SF1 `.tar.zst` | `/data/datasets/ldbc-sf1.tar.zst` |
| `${RESULTS_DIR}` | 基准报告目录 | `${PGGraph_HOME}/results` |
| `${PG_FEATURE}` | pgrx feature | `pg18`（必须匹配大版本） |

**凭据纪律**：不要把密码写进命令行、脚本或仓库；用 `.pgpass`、peer/本地信任或
运行时环境注入。示例默认无密码，仅适用于本机受控环境。

## 关键规则

1. **备份树只读**：`${PGGraph_SRC_BAK}` 是原始对照，永不修改；所有优化在
   `${PGGraph_SRC}` 进行。需要新分支时从备份复制。
2. **PostgreSQL 不能以 root 运行**：`initdb`/`pg_ctl` 用数据目录属主（通常 `postgres`）。
3. **pgrx feature 必须匹配 PG 大版本**：`pg18` ↔ `pg_config --version` 18.x；
   不匹配会在 install 时报错或装错 SQL 脚本。
4. **构建/重编译后必须重启 PostgreSQL**：新 `.so` 只在 backend 启动时加载。
5. **工件格式未变时可复用**：重启/重编译不需要重新导数据或 build；改了 CSR/工件格式
   才需要 `clean-graph` + 重新 build。
6. **`clean-graph`/`prepare` 是破坏性操作**：会删注册/工件或 DROP schema；执行前确认
   可重建、已备份，并获得用户明确同意。
7. **双向边口径**：注册时 `bidirectional := true` 会为每条边写正向+反向两条，
   `edges_loaded` 约为原始边数×2；报告必须注明。

## 工作流

```
① 依赖检查 → ② 编译安装 → ③ 启动 PG + CREATE EXTENSION
  → ④ 准备数据（下载/解压/转换）→ ⑤ 建表 + COPY
  → ⑥ 注册表/边/过滤列 → ⑦ graph.build
  → ⑧ 验收（status + 1-hop traverse + 行数/checksum）
  → ⑨ 备份工件/数据 → ⑩ 版本切换/回滚
```

## 参考索引

| 文件 | 何时读 |
|------|--------|
| `references/build-and-deps.md` | 依赖、pgrx、编译命令、产物、常见编译错误、重编译 |
| `references/topology-and-postgres.md` | PG 初始化/启动、端口、连接模型、内存/NUMA 初值、工件路径 |
| `references/data-import-and-registration.md` | LDBC/Panama 下载与转换、表结构、COPY、注册、build、验收 |
| `references/operations-troubleshooting.md` | 备份/恢复、clean-graph、工件复用、构建锁、启动/连接故障 |
| `scripts/preflight_check.sh` | 只读检查：工具链、PG、扩展、工件、catalog、基本功能 |

## 最小可运行示例

```bash
export PATH="${PGSQL_PREFIX}/bin:${HOME}/.cargo/bin:${PATH}"
export PG_FEATURE="pg$( "${PGSQL_PREFIX}/bin/pg_config" --version | sed -E 's/[^0-9]*([0-9]+).*/\1/' )"

# 1) pgrx 初始化（只需一次；路径用变量）
cargo pgrx init --"${PG_FEATURE}"="${PGSQL_PREFIX}/bin/pg_config"

# 2) 编译安装
cd "${PGGraph_SRC}"
PG_CONFIG="${PGSQL_PREFIX}/bin/pg_config" make install
# 等价：
# cd graph && cargo pgrx install --pg-config "${PGSQL_PREFIX}/bin/pg_config" \
#   --release --no-default-features --features "${PG_FEATURE}"

# 3) 启动/重启 PG（以数据目录属主执行）
su "${PGUSER}" -c "${PGSQL_PREFIX}/bin/pg_ctl -D ${PGDATA} -l ${PGDATA}/server.log restart -m fast"

# 4) 建扩展并验收
"${PGSQL_PREFIX}/bin/psql" -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" \
  -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS graph;"
```

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'graph';
SELECT * FROM graph.status();
```

## 验收判据

- `graph.so`、`graph.control`、`graph--*.sql` 位于 `pg_config --pkglibdir` 与
  `--sharedir/extension`。
- `pg_extension` 中 `graph` 版本正确。
- `graph.build()` 返回 `nodes_loaded/edges_loaded/build_time_ms/memory_used_mb`，
  且 `edges_loaded` 与“原始边数 × 双向倍数”一致。
- 一次 1-hop `graph.traverse(..., hydrate := false, max_rows := 1)` 返回 ≥1 行。
- 关键查询的 `row_count` 与 SQL 基线一致；只信 `checksum` 一致不足以证明正确。

## 输出契约

部署结论至少给出：

1. **身份**：pgGraph 版本/commit、PG 大版本、pgrx/Rust、编译命令与 feature、`.so` md5。
2. **拓扑**：PG 端口/数据目录/工作目录、连接模型、图工件路径。
3. **数据**：数据集名/规模、节点/边表数量、双向口径、build 输出。
4. **验收**：status、traverse 行数、与 SQL 基线/不变量的对账结果。
5. **风险**：是否复用旧工件、是否改过格式、回滚方式。

## 维护约定

- 命令中的路径/端口/用户一律变量化；不写个人 home、挂载盘或私网 IP。
- 数据集下载记录 URL、大小、SHA256；不把凭据写入 argv/文件。
- 版本/默认值以 `pg_config`、`graph/Cargo.toml`、`graph--*.sql` 为准；文档只给方法。
- 高风险操作（`clean-graph`、DROP schema、覆盖工作树、恢复备份）必须先备份并确认。

## 完整性校验

用例位于姊妹 skill `pggraph-architecture/cases/skill-integrity/`（随系列首 PR 交付）：

```bash
# 源码树中，且 pggraph-architecture 已合入（系列首 PR）时可执行：
bash ../pggraph-architecture/cases/skill-integrity/run.sh
# 缺失的姊妹 skill 记为 pending 而非失败；退出码 0 = 已合入成员全部通过
```

若环境中不存在该脚本（架构 skill 未合入/安装平铺布局），跳过本节，不要把
"No such file" 误报为校验失败。
