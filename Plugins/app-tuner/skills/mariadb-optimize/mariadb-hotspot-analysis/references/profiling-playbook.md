# Profiling 操作手册

本文件给出 MariaDB 热点采集的完整命令、符号准备、采样窗口、工具选择与注意事项。

## 1. 采集前检查

```
# 进程与身份
pgrep -a mariadbd
mariadb -e "SELECT VERSION(); SHOW VARIABLES LIKE 'innodb_flush_method';"

# 多实例：perf 按目标 PID 采样，SQL 连接必须指向同一实例
# 用 --socket/--port/--defaults-extra-file 指定；核对 @@pid_file 里的 PID，不一致则跳过 SQL
mariadb --port=3307 -N -e "SELECT @@pid_file"

# 是否有 debug 符号（perf 栈回溯需要）
file "$(readlink -f "$(command -v mariadbd)")"
perf --version

# CPU 频率/拓扑（避免降频/跨 NUMA 误判）
lscpu | grep -E 'Model name|MHz|NUMA|Socket'
```

要求：

- 采样窗口内不改变配置、不重启、不导入数据，也不默认启用监控计数器。
- 先 warmup（读场景 ≥5 分钟）再采样。
- 记录采样起止时间、PID、核、并发、QPS/TPS，便于对照。
- 证据目录每轮独占（非空即拒绝复用），权限 700、文件 600；默认不记录 SQL 原文
  （`INNODB_TRX` 不取 `trx_query`，死锁快照过滤语句行）。

## 2. perf：on-CPU 热点

```
PID="$(pgrep -x mariadbd | head -1)"

# 频率 999Hz，采 20s；-g 记录调用栈
perf record -F 999 -g -p "$PID" -- sleep 20

# 文本报告：self + children
perf report -g graph,0.5,callee --stdio | head -100
perf report --stdio --sort comm,dso,symbol | head -60

# 火焰图
perf script -i perf.data | \
  FlameGraph/stackcollapse-perf.pl | \
  FlameGraph/flamegraph.pl --title "mariadbd on-CPU" > oncpu.svg
```

栈展开：

- `perf record -g` 默认用 frame pointer；若二进制未保留 frame pointer，
改用 `--call-graph dwarf`，并确保编译带 `-g` 且未 strip。
`scripts/collect_profile.sh` 默认同样用 `-g`，可用 `PERF_CALLGRAPH='--call-graph dwarf'` 切换。
- `[unknown]` 高：检查 `kptr_restrict`、debug 符号包、容器内是否缺 `/proc/kallsyms` 权限。
- 多实例：`perf record -p pid1,pid2`（逗号分隔，不能有空格）。

## 3. off-CPU：等待与阻塞

```
# 按线程累计离核时长（us）
/usr/share/bcc/tools/offcputime -p "$PID" 20 > offcpu.txt
# 只看某类栈
/usr/share/bcc/tools/offcputime -p "$PID" -f 20 > offcpu_folded.txt
```

判读：

- 大量时间在 `futex`/`pthread_cond_wait` → 锁/同步等待；
- 在 `io_submit`/`pread`/`fsync` → IO 或日志刷盘等待；
- 在 `poll`/`epoll_wait` → 网络/空闲（压测下可能是客户端不足）。

## 4. Topdown / 微架构

平台支持时（鲲鹏 DevKit、x86 perf topdown）：

```
# x86 示例
perf stat -M TopdownL1 -p "$PID" -- sleep 10

# 通用指标
perf stat -e cycles,instructions,cache-misses,branch-misses,context-switches \
  -p "$PID" -- sleep 10
```

判读：

| 指标 | 含义 | 对应方向 |
| --- | --- | --- |
| Front-End Bound | 取指/分支预测受限 | 代码体积、分支、I-cache |
| Back-End Bound | 执行/访存受限 | 依赖链、除法、内存延迟 |
| Memory Bound | 访存/cache miss | 数据结构、预取、NUMA |
| IPC 低 | 每周期指令少 | 停顿多（锁/内存/分支） |
| cache-misses 高 | 缓存未命中 | 工作集、伪共享、随机访问 |

注意：Topdown 在虚拟化/容器内可能不可用或失真，结论需结合火焰图。

## 5. 系统层对照

```
mpstat -P ALL 1 30 > mpstat.txt
pidstat -p "$PID" -t 1 30 > pidstat_t.txt   # -t 按线程
iostat -x 1 30 > iostat.txt
vmstat 1 30 > vmstat.txt
sar -n DEV 1 30 > net.txt
numastat -p "$PID" > numastat.txt
```

关注：

- `%usr` vs `%sys`：内核态高 → 系统调用/锁/网络/IO。
- `iostat` 的 `await`、`aqu-sz`、`%util`：IO 饱和与排队。
- `numastat` 的 `Numa_hit`/`Numa_miss`：跨 NUMA 访问。
- 上下文切换 `cs`：线程过多或锁竞争。

## 6. InnoDB 内部证据

```
-- 总体
SHOW ENGINE INNODB STATUS\G

-- 事务与锁（不取 trx_query：语句原文含业务数据，不进证据包）
SELECT trx_id, trx_state, trx_started, trx_wait_started, trx_mysql_thread_id, trx_weight
  FROM information_schema.INNODB_TRX ORDER BY trx_started;
SELECT * FROM information_schema.INNODB_LOCK_WAITS;
-- SHOW ENGINE INNODB STATUS 归档前过滤 LATEST DETECTED DEADLOCK / TRANSACTIONS 段中
-- 跟随 "MySQL thread id" 的语句行，只保留锁与等待结构。

-- 计数器：默认只读已启用项，缺项单列，不默认 SET GLOBAL（修改会持续生效到采样之后）
-- 10.11.16 的 ENABLED 是数值列（1=已启用，0=未启用）；旧版可能仍是 STATUS（enabled/disabled）
-- 10.11 无单独日志 fsync 计数器：redo 写次数用 log_writes，fsync 总数看 os_data_fsyncs（含数据+日志）
SELECT NAME, SUBSYSTEM, ENABLED, COUNT FROM information_schema.INNODB_METRICS
 WHERE ENABLED=1 AND NAME IN ('lock_row_lock_waits','lock_row_lock_time','trx_rseg_history_len',
                'buffer_pool_reads','buffer_pool_read_requests','log_writes',
                'os_data_fsyncs','log_waits','ibuf_merges') ORDER BY SUBSYSTEM, NAME;
SELECT NAME, SUBSYSTEM, ENABLED FROM information_schema.INNODB_METRICS
 WHERE ENABLED=0 ORDER BY SUBSYSTEM, NAME;   -- 未启用/缺项
-- 旧版 STATUS 为字符串（enabled/disabled）；先探测 information_schema.COLUMNS 再分支
-- 确需启用时单独确认变更范围（仅本实例、指定模块，不用 ALL），采样后恢复原状态：
-- SET GLOBAL innodb_monitor_enable = 'module:lock';
-- ... 采样窗口 ...
-- SET GLOBAL innodb_monitor_disable = 'module:lock';

-- 缓冲池
SELECT POOL_ID, POOL_SIZE, FREE_BUFFERS, DATABASE_PAGES, MODIFIED_DATABASE_PAGES,
       PAGES_MADE_YOUNG, PAGES_NOT_MADE_YOUNG, HIT_RATE
  FROM information_schema.INNODB_BUFFER_POOL_STATS;
```

`SHOW ENGINE INNODB STATUS` 重点段：

- `BACKGROUND THREAD`、`SEMAPHORES`（等待与 spin）、`LATEST DETECTED DEADLOCK`
- `TRANSACTIONS`（活跃事务、锁等待）
- `BUFFER POOL AND MEMORY`（dirty pages、pending writes）
- `LOG`（log sequence、checkpoint、pending log writes）

归档快照时 `LATEST DETECTED DEADLOCK` 与 `TRANSACTIONS` 段里跟随 `MySQL thread id` 的
语句行应替换成占位符（如 `[SQL 语句已过滤]`），只保留锁/等待结构，避免业务 SQL 原文
进入证据包。

## 7. performance_schema（若启用）

```
-- 线程
SELECT THREAD_ID, NAME, TYPE, PROCESSLIST_STATE, PROCESSLIST_INFO
  FROM performance_schema.threads WHERE TYPE='FOREGROUND';

-- 等待汇总
SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT/1e12 AS sec
  FROM performance_schema.events_waits_summary_global_by_event_name
 ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;

-- 语句汇总（按 digest）
SELECT DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT/1e12 AS sec,
       SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_CREATED_TMP_DISK_TABLES
  FROM performance_schema.events_statements_summary_by_digest
 ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;
```

注意：MariaDB 的 `performance_schema` 默认关闭；开启会增加内存与少量 CPU，
A/B 两腿必须一致。容器内开启还受 `performance_schema_max_*` 与内存限制影响。

## 8. 采样窗口与轮次

- perf / off-CPU / pidstat / mpstat / iostat / sar 对同一 `--seconds` 窗口并发启动，
  记录起止时间；累计计数（`SHOW GLOBAL STATUS`、`INNODB_METRICS`）在窗口两端各读一次，
  用增量/实际秒数解释。串行等待同一窗口会让六组数据横跨数分钟，系统指标和等待栈
  可能来自与 perf 热点不同的负载阶段。
- 每个场景至少采 2 个窗口（如第 2–3 分钟、第 8–9 分钟），确认热点稳定。
- 变化中的系统（预热、compaction、purge 追赶）要标注“非稳态”。
- 采样之间不重置数据（否则不可比）；跨轮对比要回到同一基线状态。

## 9. 常见坑

| 坑 | 说明 |
| --- | --- |
| `perf -p` 用了空格分隔 | 必须逗号：`pid1,pid2` |
| `-g dwarf` 写成位置参数 | `-g` 不接收展开方式，应写 `--call-graph dwarf` |
| 采样时同时开 PFS 并对比 | PFS 本身有开销，两腿要一致 |
| 只采一个核 | 数据库线程会迁移；按 PID 采或固定绑核后再采 |
| 容器内看不到内核符号 | 补 `kptr_restrict`/debug 包，或只在用户态判读 |
| 把 idle 等待当热点 | off-CPU 的 `poll` 可能是客户端不足 |
| 忽略版本差异 | 10.11 的锁实现与 10.3 不同，旧热点名可能已消失 |

## 10. 证据包

一次分析至少保留：

```
perf.data / oncpu.svg / offcpu.txt
mpstat.txt / pidstat_t.txt / iostat.txt / numastat.txt
status.before / status.after / innodb_metrics.before / innodb_metrics.after / window.txt
innodb_status.txt（语句行已过滤）/ innodb_metrics.txt（仅已启用项）
innodb_metrics_missing.txt / innodb_trx.txt（无 SQL 原文）/ lock_waits.txt
场景与身份说明（并发、时长、数据、md5、绑核）
性能数字（TPS/QPS/p99/CPU）
结论与待验证假设

每轮使用独占目录（非空即拒绝复用），目录 700、文件 600；`sql_skip.txt` 记录跳过原因，
`result.status` 记录 ok/no_data，避免上一轮产物被拼进本轮报告。
```

## 11. 常见误判（从 SKILL.md 下沉的完整表）

| 误判 | 纠正 |
| --- | --- |
| “CPU 没满，所以不是瓶颈” | 可能在等锁/IO/日志刷盘；看 off-CPU 与等待事件 |
| “火焰图里 futex 高就是数据库 bug” | 高并发下 futex 是正常同步开销，要看是否可减少共享写 |
| “`[unknown]` 是无效数据” | 多为内核/vdso/缺符号；补 debug 符号再判定 |
| “self 低就不重要” | children 可能很高；要看调用路径聚合 |
| “CPU <数值>% 就是计算瓶颈” | 可能大量时间在自旋/原子/内存分配 |
| “P99 高就是慢查询” | 可能是 checkpoint、purge、刷脏、网络抖动 |
| “Topdown Memory Bound 就要加内存” | 可能是 cache miss/访存模式/伪共享 |
| “压测数字低就是服务端慢” | 先排除客户端 CPU/网络/连接池 |
| “10.3 的热点补丁在 10.11 一样有效” | 锁实现已重写，必须重新验证 |
| “`oltp_update_non_index` 是全表扫描，要加索引” | 该 lua 用 `UPDATE ... WHERE id=?` 走主键点查，无索引的是被更新的 `c` 列；热点在行锁/redo/undo，不是全表扫描 |

## 12. 热点分析报告模板

```
## 热点分析报告

### 场景与身份
- 场景/工具/并发/时长/warmup
- 数据规模、部署形态、实例数、线程模型、绑核
- 二进制 md5/commit、编译参数、分配器

### 性能对照
- TPS/QPS、错误率、p50/p99、各进程 CPU、IO

### 线程/阶段分解
- 连接/线程池、page cleaner、purge、log、IO 线程占比
- 锁等待、redo 等待、刷脏、历史列表长度（有数据则列）

### Top 热点
| 排名 | self% | children% | 函数 | 线程角色 | 机制 | 证据 |

### 归因
- 瓶颈线程 → 机制 → 为什么（对照/实验）
- 与性能数字的对应关系

### 优化方向
- 按“部署 / 配置 / 编译 / 源码 / 第三方”分类，给预期收益与验证方式
- 明确哪些是假设，哪些已被对照验证
```
