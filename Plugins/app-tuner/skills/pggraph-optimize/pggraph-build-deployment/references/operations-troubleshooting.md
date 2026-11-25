# 运维、复用与故障排查

## 1. 日常状态检查

```bash
# PG 就绪
"${PGSQL_PREFIX}/bin/pg_isready" -h "${PGHOST}" -p "${PGPORT}"

# 扩展版本
psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" -At \
  -c "SELECT extversion FROM pg_extension WHERE extname='graph';"

# 工件
find "${PGDATA}" -name main.pggraph -printf '%p %s bytes %TY-%Tm-%Td %TH:%TM\n'

# catalog
psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" -At \
  -c "SELECT count(*) FROM graph._registered_tables;"
```

`scripts/preflight_check.sh` 把这些只读检查串起来；默认不改任何状态。

## 2. 工件复用

| 操作 | 工件 | 需要重建？ |
|------|------|------------|
| 重启 PG | 保留 | 否 |
| 重新编译 `.so`（格式未变） | 保留 | 否 |
| 改 CSR/工件格式 | 不可用 | 是 |
| 改注册表/边/过滤列 | 旧工件不匹配 | 是 |
| 源表数据变更 | 由 sync/overlay 或重建处理 | 视一致性要求 |
| 换数据集 | 旧工件无意义 | 是 |
| 迁移到另一实例 | 格式/版本一致可复用 | 否则重建 |

复用前核对：

1. pgGraph 版本/工件 version 一致。
2. 注册的 table OID 在目标库仍有效（OID 不是跨库稳定标识；重建更安全）。
3. 源表 schema 未漂移。
4. 重启后一次 1-hop traverse 成功，且 row_count 与 SQL 基线一致。

**不要**只因为 `graph.status()` 显示 0 就重建；先用 traverse 验证。

## 3. clean-graph 与重新构建

破坏性操作，先确认：

```sql
-- graph.reset() 只清引擎/工件（与架构 skill 口径一致）；图注册（catalog 表）
-- 由下方 TRUNCATE 清空，源表不动——不要指望 reset 兜底清注册
SELECT graph.reset();
TRUNCATE graph._registered_filter_columns,
         graph._registered_edges,
         graph._registered_tables,
         graph._build_jobs,
         graph._maintenance_jobs,
         graph._sync_log
RESTART IDENTITY;
-- graph._sync_buffer 在部分版本/模式下不存在（见 guc-and-data-model 的表清单），
-- PostgreSQL 无 TRUNCATE IF EXISTS、任一表缺失即整条 42P01 失败，故先探测再单独补：
SELECT CASE WHEN to_regclass('graph._sync_buffer') IS NOT NULL
       THEN format('TRUNCATE %s RESTART IDENTITY', 'graph._sync_buffer')
       END \gexec
```

然后重新执行注册脚本 + `graph.build()`。适用场景：

- 工件格式变更/校验失败。
- 注册表漂移、重复注册、边标签混乱。
- 构建中断留下不一致状态。
- 换数据集。

不适用：只是想读最新源表 → 先 `apply_sync`/维护；只是性能慢 → 先做热点分析。

## 4. 构建锁与中断

`graph.build()` 中断后可能留下锁/后台任务：

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS duration, left(query, 120) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle'
ORDER BY duration DESC;
```

确认是残留 build 后再：

```sql
SELECT pg_terminate_backend(<pid>);
```

不要直接删 `main.pggraph.tmp`（持久化先写 `<path>.tmp` 再原子 rename，`<path>` 即 main.pggraph）或手工改 header；下一次 build 会覆盖/校验。

## 5. 常见故障

### 5.1 启动/连接

| 现象 | 原因 | 处理 |
|------|------|------|
| `could not connect`/`Connection refused` | PG 未启动/端口错 | `pg_isready`、`ss -ltnp`、检查 `port` |
| `Address already in use` | 端口被占用 | 换端口或停冲突进程 |
| `permission denied` 数据目录 | 属主/权限错 | 用数据目录属主运行，修正权限 |
| `root` 启动失败 | PG 禁止 root | `su postgres -c ...` |
| 重启后扩展未加载 | 未重启/装错前缀 | 重启、核对 `pg_config` 与 `graph.so` |

### 5.2 扩展/构建

| 现象 | 原因 | 处理 |
|------|------|------|
| `extension "graph" is not available` | 产物未装到当前 PG | 核对 `--pkglibdir/--sharedir`，重装 |
| `graph.build` OOM/降级只读 | `memory_limit_mb` 太小 | 提高限制或缩小数据集；记录 |
| build 报工件校验失败 | 格式/CRC/section 异常 | 重新 build；不要手改 |
| `PG018` | 加权路径有 pending overlay | `graph.vacuum()`/`maintenance()` |
| GQL row cap | 结果超 10000 | 收窄查询/加 LIMIT；不是环境故障 |
| `graph.status()` 为 0 | status 是内存元数据 | 执行一次 traverse |

### 5.3 数据/结果

| 现象 | 原因 | 处理 |
|------|------|------|
| 遍历少节点 | `max_rows/max_depth/max_frontier` 截断 | 放大限制后重测，并记录 |
| search 结果为空 | 列未注册/模式不匹配/索引缺失 | 查 `registered_tables`、换 mode、看执行计划 |
| 图与源表不一致 | 同步延迟/未 apply_sync | 查 `_sync_log`、`query_freshness`、事务边界 |
| 边数翻倍 | 双向注册 | 报告注明 `edges_loaded` 口径 |
| 结果 checksum 不稳定 | 非确定性/并发/数据变化 | 固定数据/事务/连接；查是否用了 LIMIT 无序 |
| 重启后首次慢 | 每 backend 加载 + 反向 CSR | 预热/连接池；不要计入稳态 |

### 5.4 编译

见 `references/build-and-deps.md` 的错误表；最常见是 `postgres.h` 缺失和 pgNN feature 不匹配。

## 6. 版本切换与回滚

```
① 记录当前身份：pgGraph 版本/commit、.so SHA256、PG/pgrx/Rust、工件 version
② 备份：源表（pg_dump）、catalog（随 dump）、可选工件目录，以及**旧二进制套件**——
   从 pkglibdir 保存旧 graph.so 与 sharedir/extension 下的 graph.control/graph--*.sql
   到备份目录（只有 SHA256 摘要无法恢复二进制），备份后校验非空并比对 SHA256；
   任一备份失败或产物为空时中止切换，不得继续替换；
   记录实际备份文件路径与 .so SHA256 供第 ⑤ 步回滚
③ 切换源码/二进制 → 重新编译 → 重启
④ 验收：扩展版本、1-hop traverse、row_count/checksum、关键查询
⑤ 失败回滚：用第 ② 步记录的备份恢复二进制 + 重启；工件格式不兼容则恢复备份或重建
```

回滚判据：

- 功能失败（traverse 报错、结果与 SQL 基线不一致）→ 立即回滚。
- 性能负收益且无法在单变量下解释 → 回滚，保留证据。
- 工件格式变更后无法加载 → 回滚或重建，不能混用旧工件。

## 7. 日志与证据

| 证据 | 位置/方式 |
|------|-----------|
| PG 日志 | `${PGDATA}/server.log` 或 `log_directory` |
| 扩展 NOTICE/ERROR | PG 日志/客户端 stderr |
| 构建输出 | `graph.build()` 返回 JSON、`report.json.build_output` |
| 基准报告 | `${RESULTS_DIR}/<run_id>/report.json` |
| SQL 清单 | `report.json.sql_manifest`（含 SQL + SHA256） |
| 机器信息 | `lscpu`、`numactl -H`、`/proc/meminfo`、`pg_config --version` |
| 二进制身份 | `sha256sum graph.so` |

报告时把“环境/数据/口径/结论/限制”分开写，避免把一次实测当成通用规格。
