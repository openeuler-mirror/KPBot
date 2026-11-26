# Profiling 采集手册

## 1. 采集原则

1. **先固定场景**：同一条 SQL、同一 seed、同一 limit/hydrate、同一连接数和绑核。
2. **先粗后细**：总览（谁在跑/等）→ 进程/核 → 函数 → 微架构 → 内存。
3. **排除客户端**：服务端 CPU 不高时，先看客户端 CPU、GIL、连接提交速率。
4. **保留原始数据**：perf.data、火焰图、pg_stat 快照、smaps、配置、`.so` SHA256。
5. **不要在生产高峰做重采样**；perf/火焰图有开销，先确认窗口。

## 2. 环境与权限

```bash
# perf 权限（需要时由管理员调整；不要在生产随意改内核参数）
cat /proc/sys/kernel/perf_event_paranoid

# 版本/身份
uname -a
lscpu
numactl -H
"${PGSQL_PREFIX}/bin/pg_config" --version
sha256sum "$( "${PGSQL_PREFIX}/bin/pg_config" --pkglibdir )/graph.so"

# 进程
ps -eo pid,ppid,comm,psr,pcpu,pmem,args --sort=-pcpu | head -30
```

## 3. 总览：谁在跑、谁在等

```sql
-- 活跃/等待/ClientRead 分布
SELECT state, wait_event_type, wait_event, count(*)
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1,2,3
ORDER BY 4 DESC;

-- 长查询
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS duration,
       left(query, 160) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle'
ORDER BY duration DESC
LIMIT 20;
```

`pg_stat_statements`（如已安装）：

```sql
-- dbid 过滤到当前数据库（与 collect_profile.sh 同口径）：pg_stat_statements 汇总整个
-- 实例，跨库读取会把其他库的 SQL 文本带进证据包、全实例 Top20 也会挤掉目标库慢查询
SELECT queryid, calls, total_exec_time, mean_exec_time, rows
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC
LIMIT 20;
```

需要 SQL 文本时再按 queryid 联查 `pg_stat_statements.queryid → query`（作为可选步骤，
证据包默认只留 queryid 与数值指标）。

注意：`pgGraph` 的 `graph.*` 是表函数，`pg_stat_statements` 会记录外层 SQL；
函数内部时间可通过 `EXPLAIN (ANALYZE)` 或 perf 看到。

## 4. 进程/核

```bash
# 每核利用率（排除 idle 核干扰时用 -C）
mpstat -P ALL 1 10

# 单进程 CPU/内存
pidstat -u -r -p "${BACKEND_PID}" 1 10

# 客户端进程（Python 等）
ps -eo pid,comm,pcpu,psr,args | grep -E 'python|psql|java' | grep -v grep
```

判读：

- 服务端只有少数核高：可能是单 backend/单查询、锁/SPI 串行、或客户端喂不动。
- 客户端 ~100% 单核：Python GIL/单线程客户端。
- 每核 30-50% 但吞吐不涨：SMT/排队/缓存争用，结合 Topdown。

## 5. 函数级：perf + 火焰图

```bash
# 采样一个 backend（PID 从 pg_stat_activity 拿）
perf record -F 99 --call-graph dwarf -p "${BACKEND_PID}" -- sleep 30
perf report -i perf.data --stdio --no-children -g none --percent-limit 0.5

# 全核采样（注意开销；CPULIST 用实际绑定核）
perf record -F 99 -C "${CPULIST}" -g --call-graph dwarf -o perf.data -- sleep 30

# 火焰图（需要 FlameGraph 脚本）
perf script -i perf.data | \
  /path/to/FlameGraph/stackcollapse-perf.pl | \
  /path/to/FlameGraph/flamegraph.pl > flamegraph.svg
```

Rust 符号：

- release + LTO 可能内联/裁剪符号；保留 `graph.so` 的符号表，或在受控构建中加
  `debug = 1`/`split-debuginfo` 后复测。
- `perf report` 看不到函数名时，先确认 `perf` 能读 `graph.so` 的符号，再用
  `nm -C graph.so | grep <name>` 辅助。

常见调用栈（用于对照）：

```
component_stats（单行聚合，链尾无逐节点 String）:
  component_stats_wrapper → with_panic_boundary → Engine::connected_components
  → compute_components_with_neighbors → UnionFind::union/find
  → component_sizes 计数 / active_count
  （to_component_rows 的逐节点 to_string() 属 graph.connected_components() 逐行 API）

traverse:
  traverse_wrapper → execute_traverse_candidates
  → ENGINE.with → traverse_with_filter_ops → bfs::execute_with_neighbors
  → candidate_allowed (visited/edge_type/active/tx_delta/tenant/filter)   # 六重检查，与 scenario-hotspots 的口径一致
  → to_traversal_results (String/path)
  → sort_by(depth, table, node_id)
  → hydrate_nodes (SPI) / JSON / regclass_text
```

## 6. 微架构：perf stat / Topdown

```bash
# 单 backend
perf stat -p "${BACKEND_PID}" -e cycles,instructions,cache-references,cache-misses,\
branch-misses,context-switches,cpu-migrations -- sleep 10

# 一组核（排除 idle 核）
perf stat -C "${CPULIST}" -e cycles,instructions,cache-references,cache-misses -- sleep 10
```

aarch64 上事件名/PMU 可能不同；如果通用事件不可用，使用厂商工具（如鲲鹏 DevKit
Topdown）或 `perf list` 查可用事件。关注：

- IPC（instructions/cycle）：下降说明 stall/争用。
- L1/L2/L3 miss、DRAM local/remote。
- backend stall vs frontend stall。
- TLB walk、context switch、CPU migration。

Topdown 判读：

- Frontend bound：指令取指/分支，图算法通常不是主因。
- Backend bound / memory bound：CSR 随机访问、cache miss、NUMA。
- Bad speculation：分支/间接跳转；图遍历有，但通常小于内存。
- Retiring：有效计算；若很高但吞吐不涨，看算法/工作量。

## 7. 内存：共享 mmap vs 私有堆

```bash
# PSS/Private（推荐）
grep -E '^(Pss|Private_Clean|Private_Dirty|Rss):' /proc/${BACKEND_PID}/smaps_rollup

# 映射级：区分 .pggraph mmap 与匿名堆
grep -A6 -E 'pggraph|\[heap\]|anon' /proc/${BACKEND_PID}/smaps | head -80

# NUMA 分布
numastat -p "${BACKEND_PID}"
```

要点：

- `.pggraph` 的 RSS 会重复计入每个 backend，但物理页可共享；用 PSS 分摊。
- 反向 CSR/FilterIndex 是私有堆，随 backend 数放大。
- BFS 状态/结果 String 是查询级，短生命周期但可能很大（2-hop、hydrate）。

## 8. SQL/SPI/hydrate

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT * FROM graph.traverse(
  'ldbc.persons'::regclass, :'seed', 2,
  hydrate := true, max_rows := 1000);
```

关注：

- `Planning Time` / `Execution Time`；函数内部时间在 Execution 里。
- `Buffers: shared hit/read`；hydrate 会读源表页。
- 是否走 Seq Scan/Index Scan；hydrate 的 `= ANY($1)` 是否有合适索引。
- `hydrate=false` 对比，量化 SPI/JSON 开销。

SPI 是单 backend 内同步串行：一个 hydrate 查询完成前，当前 backend 不能做别的图计算。
高并发下每个 backend 各跑各的 SPI，但单查询内的 hydrate 无法与 BFS 重叠。

## 9. 客户端

```bash
# Python 进程 CPU
ps -p <CLIENT_PID> -o pid,pcpu,pmem,psr,args
cat /proc/<CLIENT_PID>/stat | awk '{print "utime+stime=" $14+$15}'

# 连接数/状态
ss -tnp | grep ":${PGPORT}" | wc -l
```

若客户端是 Python threading：

- 查询执行时 psycopg 释放 GIL，但查询构造/结果解析仍持 GIL。
- 观察客户端 CPU 是否 ~1 核、backend 是否大量 `ClientRead`。
- 用 `--mp` 多进程复测；若 QPS 明显上升，原瓶颈是客户端。

## 10. 采集产物

以 `collect_profile.sh` 实际产出为准（与脚本头部清单一致）：

```
${RESULTS_DIR}/profile-<run_id>/
  env.txt                      # uname/lscpu/numactl/pg_config/.so sha256
  pg_stat_activity_start.txt   # 压测窗口起点快照
  pg_stat_activity_end.txt     # 窗口终点快照
  pg_stat_statements_start.txt # 如可用（pgss 已加载且预检通过）
  pg_stat_statements_end.txt
  window.txt                   # 采样时间窗口（起止时间戳）
  perf-stat.txt                # perf stat 计数器（--run 且 PMU 可用时）
  perf-record.txt              # perf record 运行日志
  perf.data
  perf-report.txt              # perf report 文本化
  smaps-rollup.txt             # 目标 PID 内存布局汇总（PID 归属核实时；远程/非 postgres 进程记 skip）
  summary.txt                  # 逐项采集状态（psql/pgss/perf/smaps），失败项 → 退出码 1
```

以下为**手工补充项**（脚本不产出，报告阶段自行生成）：

```
  flamegraph.svg                # perf.data 离线渲染（flamegraph.pl 等工具）
  explain-<query>.json         # 手工 EXPLAIN（XML/JSON 导出）
  notes.md                      # 场景、时间窗口、异常
```

采样时间窗口要覆盖稳态（避开 warmup/加载），并在报告里写明。
