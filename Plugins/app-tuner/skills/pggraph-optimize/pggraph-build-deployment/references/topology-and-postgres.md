# PostgreSQL 拓扑、启动与部署

## 1. 部署形态

pgGraph 跑在 PostgreSQL 后端进程内，没有独立服务进程。部署对象是：

```
PostgreSQL 实例
  ├─ postmaster（监听端口）
  ├─ backend（每连接一个；每个 backend 一份 thread_local Engine）
  ├─ background worker（异步 build/maintenance）
  └─ PGDATA
       └─ <graph.data_dir>/<graph_id>/main.pggraph
```

因此：

- “扩容 pgGraph”主要是增加/复用 PostgreSQL 连接与后端，不是加图服务节点。
- 每 backend 私有内存（反向 CSR、FilterIndex、查询状态）会随连接数放大；
  连接池通常比 `max_connections` 无上限更有效。
- 图工件在 `PGDATA` 下，跟 PG 的数据目录一起备份/迁移；它不在 WAL 里，是可重建的派生状态。

## 2. 初始化与启动

PostgreSQL 不能以 root 运行。用数据目录属主（如 `postgres`）：

```bash
# 仅首次
su "${PGUSER}" -c "${PGSQL_PREFIX}/bin/initdb -D ${PGDATA}"

# 启动/重启
su "${PGUSER}" -c "${PGSQL_PREFIX}/bin/pg_ctl -D ${PGDATA} -l ${PGDATA}/server.log start"
su "${PGUSER}" -c "${PGSQL_PREFIX}/bin/pg_ctl -D ${PGDATA} -l ${PGDATA}/server.log restart -m fast"

# 就绪检查
"${PGSQL_PREFIX}/bin/pg_isready" -h "${PGHOST}" -p "${PGPORT}"
```

如果已有实例，不要重复 `initdb`；只重启加载新 `.so`。

## 3. 连接与认证

```bash
"${PGSQL_PREFIX}/bin/psql" -h "${PGHOST}" -p "${PGPORT}" -U "${PGUSER}" -d "${PGDB}" \
  -v ON_ERROR_STOP=1 -c "SELECT version();"
```

凭据处理：

- 本机受控环境可用 peer/trust；远程用 `.pgpass`（权限 600）或运行时注入。
- 不要把密码写进 `psql "postgresql://user:pass@..."`、脚本、日志或仓库。
- 基准脚本的 `--password` 参数在示例中留空；需要密码时用环境/`.pgpass`。

## 4. 端口与目录

| 项 | 建议 | 说明 |
|----|------|------|
| `${PGPORT}` | 自定义（示例 6666） | 避免与系统默认 5432 冲突；Docker 映射端口另算 |
| `${PGDATA}` | 独立数据盘 | 与源码、结果目录分离；确保属主正确 |
| `${PGGraph_SRC}` | 源码/工作树 | 不放在 `PGDATA` 内，避免备份/权限混淆 |
| `${RESULTS_DIR}` | 独立结果目录 | 记录 run_id、report.json、日志 |
| `graph.data_dir` | 默认 `graph` | 图工件根目录；多图按 `graph_id` 分目录 |

端口冲突排查：

```bash
ss -ltnp | grep -E ":${PGPORT}\b" || true
```

pgGraph 本身不额外监听端口；冲突通常来自 PG 实例、Docker 映射或其他服务。

## 5. PostgreSQL 初始参数

以下是“能跑起来”的起点，不是性能结论；性能参数由
`pggraph-performance-optimization` 的单变量实验决定。

```conf
# postgresql.conf（示例，按内存/负载调整）
listen_addresses = 'localhost'
port = 6666
max_connections = 256
shared_buffers = '8GB'
work_mem = '64MB'
maintenance_work_mem = '1GB'
effective_cache_size = '24GB'
jit = off
track_activity_query_size = 4096
```

说明：

- `shared_buffers` 是 PG 共享缓冲，和 `.pggraph` mmap 是两套内存，不要混算。
- `work_mem` 影响 SQL JOIN 基线、排序/哈希；A/B 时必须固定。
- `jit=off` 对 pgGraph 查询可能只有约 1% 收益，且依赖版本/负载；先测再定。
- `max_connections` 不是越大越好：每 backend 一份 Engine 私有内存，连接数超过
  活跃并发只会增加缓存争用。
- 大页/THP/NUMA 策略属于 OS 层，交给 `kpbot-app-tuner` 的 `os-optimization` /
  `cpu-affinity-optimization`；本 skill 只记录绑定方式。

## 6. 内存与 NUMA 初值

单机压测建议先记录：

| 项 | 采集 |
|----|------|
| CPU 拓扑 | `lscpu -e`、`numactl -H` |
| L3/CCL | `lscpu`、厂商工具；鲲鹏注意 L3 tag directory/CCL |
| 内存 | `/proc/meminfo`、`numastat` |
| 每 backend 私有内存 | `/proc/<pid>/smaps_rollup` 的 `Pss`/`Private_*` |
| 工件大小 | `du -h ${PGDATA}/graph/**/main.pggraph` |

绑定示例（仅示意，CPU 列表按实际拓扑替换）：

```bash
numactl -C <CPU_LIST> -m <NUMA_NODE> "${PGSQL_PREFIX}/bin/postgres" -D "${PGDATA}" ...
# 或在已启动实例上把 benchmark 客户端绑到另一组核，避免客户端与服务端争核
```

不要盲目绑单核：单核基线用于定位，不代表生产吞吐；也不要让客户端和服务端抢同一组核。

## 7. 图工件生命周期

| 阶段 | 动作 | 检查 |
|------|------|------|
| 首次构建 | `graph.build()` | 返回节点/边/耗时/内存 |
| 持久化 | `graph.persist_on_build=true` | `main.pggraph` 存在且大小合理 |
| 重启 | 自动/显式加载 | 首次查询预热，status 可能为 0 |
| 重编译 | 重启加载新 `.so` | 工件格式未变则复用 |
| 换数据/注册 | 重新 build/vacuum | 旧工件失效 |
| 格式变更 | `clean-graph` + build | 先备份，确认可重建 |
| 迁移 | 拷贝 `PGDATA` 或只拷源表 + 重建 | 工件路径随 data_dir |

## 8. 部署验收清单

- [ ] `pg_isready` 成功，`SELECT version()` 与预期大版本一致。
- [ ] `pg_extension` 中 `graph` 版本正确。
- [ ] `graph.so` SHA256 已记录。
- [ ] `graph.status()` 可调用；一次 1-hop traverse 返回 ≥1。
- [ ] `graph._registered_tables/edges` 数量与注册脚本一致。
- [ ] `main.pggraph` 存在，大小与 `memory_used_mb`/边数量级相符。
- [ ] `report.json`/日志目录可写，run_id 可追溯。
- [ ] 备份策略覆盖源表 + `PGDATA`（工件可选，可重建）。
