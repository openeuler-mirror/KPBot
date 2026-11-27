# 状态计数器、INNODB_METRICS、PFS 与 Topdown

本文件把“看什么数字”系统化：全局状态、InnoDB 内部计数、性能模式、Topdown 与
优化优先级。采集要取差值并按时间归一化，避免把累计值当瞬时值。

## 1. 采集方法

```
# 采样前后各一次
mariadb -e "SHOW GLOBAL STATUS" > status.before
# ... 压测窗口 ...
mariadb -e "SHOW GLOBAL STATUS" > status.after

# 求增量（示意：用 awk 对齐同名计数器）
join -j 1 \
  <(awk 'NR>1{print $1"\t"$2}' status.before | sort) \
  <(awk 'NR>1{print $1"\t"$2}' status.after  | sort) \
  | awk '{d=$3-$2; if(d!=0) print $1"\t"d}' | sort -k2 -nr | head -40
```

要求：记录采样起止时间与并发，所有速率 = 增量 / 秒数。perf/offcpu/pidstat/mpstat/iostat
等对照采集器对同一窗口并发启动、记录起止时间；累计计数在窗口两端读取（status.before /
status.after），不要串行等待同一窗口导致各通道来自不同负载阶段。

## 2. 全局状态重点

| 计数器 | 含义 | 关注点 |
| --- | --- | --- |
| `Questions`/`Queries` | 语句量 | QPS 口径差异（存储过程内语句） |
| `Com_commit`/`Com_rollback`/`Com_stmt_execute` | 事务/预编译 | TPS 与连接池行为 |
| `Threads_connected`/`Threads_running` | 连接/活跃 | 排队与并发 |
| `Threads_created` | 新建线程 | 线程缓存不足 |
| `Innodb_buffer_pool_read_requests` | 逻辑读 | 工作集与访问次数 |
| `Innodb_buffer_pool_reads` | 物理读 | 未命中，缓存不足 |
| `Innodb_buffer_pool_pages_dirty` | 脏页 | 刷脏压力 |
| `Innodb_log_waits` | redo 空间等待 | 写抖动强信号 |
| `Innodb_log_write_requests`/`Innodb_log_writes` | redo 写 | 写入规模 |
| `Innodb_rows_read/inserted/updated/deleted` | 行操作 | SQL 实际工作量 |
| `Innodb_row_lock_waits`/`time` | 行锁等待 | 冲突程度 |
| `Innodb_pages_created` | 新建页 | 页分裂/插入模式 |
| `Created_tmp_tables`/`Created_tmp_disk_tables` | 临时表 | 排序/子查询代价 |
| `Sort_merge_passes` | 归并排序轮次 | filesort 落盘 |
| `Handler_read_*` | 访问方式 | 全表/索引/回表 |
| `Select_scan`/`Select_full_join` | 扫描/无索引连接 | SQL/索引问题 |
| `Table_open_cache_misses` | 打开表未命中 | table_open_cache 不足 |
| `Aborted_connects`/`Aborted_clients` | 连接异常 | 网络/超时/池配置 |
| `Binlog_cache_disk_use` | binlog 缓存落盘 | 大事务 |
| `Slave_open_temp_tables` | 复制 | 临时表（状态变量，可差分） |

> `Seconds_Behind_Master` 是 `SHOW SLAVE/REPLICA STATUS` 的输出列，**不是** `SHOW GLOBAL
> STATUS` 状态变量：按 §1 对全局状态做差分该行恒为空，复制延迟需单独归档从库状态快照。

## 3. INNODB_METRICS

默认多数计数器未启用；只读诊断默认只读已启用项并单列缺项，不修改全局监控状态
（`SET GLOBAL innodb_monitor_enable` 会在采样结束后继续生效）。MariaDB 10.11.16 的
`ENABLED` 是数值列（1=已启用，0=未启用），旧版可能仍是 `STATUS`（enabled/disabled），按实际列分支：

```
-- 10.11.16：已启用计数器（ENABLED=1）
SELECT NAME, SUBSYSTEM, ENABLED, COUNT FROM information_schema.INNODB_METRICS
 WHERE ENABLED=1 ORDER BY SUBSYSTEM, NAME;
-- 未启用/缺项（ENABLED=0），单独记录，不在只读入口补齐
SELECT NAME, SUBSYSTEM, ENABLED FROM information_schema.INNODB_METRICS
 WHERE ENABLED=0 ORDER BY SUBSYSTEM, NAME;
-- 旧版 STATUS 为字符串（enabled/disabled）；先探测 information_schema.COLUMNS 再分支
```

确需启用时必须单独确认变更范围（仅本实例、指定具体模块，不用 ALL），采样后恢复原状态：

```
SET GLOBAL innodb_monitor_enable = 'module:lock';   -- 例：只启用 lock 模块
-- ... 采样窗口 ...
SET GLOBAL innodb_monitor_disable = 'module:lock';  -- 恢复，避免全局状态残留
```

常用计数器：

| NAME | 含义 |
| --- | --- |
| `lock_row_lock_waits` / `lock_row_lock_time` | 行锁等待次数/时间 |
| `lock_deadlocks` | 死锁次数 |
| `trx_rseg_history_len` | 历史列表长度（purge 滞后） |
| `buffer_pool_reads` / `buffer_pool_read_requests` | 物理/逻辑读 |
| `buffer_pool_pages_dirty` | 脏页 |
| `log_writes` / `os_data_fsyncs` | redo 写次数 / fsync 总数（10.11 无单独日志 fsync 计数器，后者含数据+日志） |
| `log_waits` | redo 等待 |
| `ibuf_merges` | change buffer 合并 |
| `innodb_dblwr_writes` | 双写写次数 |

注意：计数器是累计值；启用 monitor 本身有少量开销，A/B 两腿要一致。

## 4. performance_schema（若启用）

```
-- 等待事件 Top
SELECT EVENT_NAME, COUNT_STAR,
       ROUND(SUM_TIMER_WAIT/1e12,3) AS seconds,
       ROUND(SUM_TIMER_WAIT/NULLIF(COUNT_STAR,0)/1e9,3) AS avg_ms
  FROM performance_schema.events_waits_summary_global_by_event_name
 WHERE COUNT_STAR > 0
 ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;

-- 语句 digest Top
SELECT DIGEST_TEXT, COUNT_STAR,
       ROUND(SUM_TIMER_WAIT/1e12,3) AS seconds,
       SUM_ROWS_EXAMINED, SUM_ROWS_SENT,
       SUM_CREATED_TMP_DISK_TABLES, SUM_NO_INDEX_USED
  FROM performance_schema.events_statements_summary_by_digest
 ORDER BY SUM_TIMER_WAIT DESC LIMIT 20;

-- 当前阻塞（MariaDB 10.11 无 MySQL 8.0 的 data_lock_waits 表）
-- 行锁等待：information_schema.INNODB_LOCK_WAITS / SHOW ENGINE INNODB STATUS
-- MDL 等待：performance_schema.metadata_locks 的 PENDING 行
SELECT * FROM performance_schema.metadata_locks WHERE LOCK_STATUS='PENDING';
SELECT * FROM performance_schema.threads
 WHERE PROCESSLIST_STATE IS NOT NULL AND TYPE='FOREGROUND';
```

PFS 的常见等待类：

| 等待 | 含义 |
| --- | --- |
| `wait/io/file/innodb/innodb_log_file` | redo 文件 IO |
| `wait/io/file/innodb/innodb_data_file` | 数据文件 IO |
| `wait/io/table/sql/handler` | 引擎层表 IO |
| `wait/lock/metadata/sql/mdl` | 元数据锁 |
| `wait/synch/mutex/innodb/*` | InnoDB mutex |
| `wait/synch/rwlock/innodb/*` | InnoDB rwlock |
| `wait/io/socket/sql/*` | 客户端/复制网络 |

## 5. Topdown 与微架构

| 指标 | 高值含义 | 优化方向 |
| --- | --- | --- |
| Front-End Bound | 取指/分支受限 | 减少分支、代码布局、I-cache |
| Back-End Bound | 执行/访存受限 | 依赖链、内存延迟、NUMA |
| Memory Bound | 访存/cache miss | 数据结构、预取、分片 |
| Core Bound | 执行单元压力 | 算法/指令选择 |
| IPC | 每周期指令数 | 低 → 停顿多 |
| LLC miss | 末级缓存未命中 | 工作集、伪共享 |

判读纪律：

- Topdown 是“为什么 CPU 效率低”，不是“慢在哪”；要结合火焰图定位函数。
- 虚拟化/容器内可能不可用或失真，需标注环境。
- 单核采样代表性有限；按数据库绑核范围抽样多个核。

## 6. 指标到优化方向的映射

| 证据 | 首选方向 | 次选 | 避免 |
| --- | --- | --- | --- |
| `Innodb_buffer_pool_reads` 高 | 增大 pool / 覆盖索引 | 分实例 | 盲目加 CPU |
| `Innodb_log_waits` 高 | 增大 redo / io_capacity | 分盘、组提交 | 只加 buffer pool |
| 脏页高、pending writes | 提高刷脏能力 | 更快盘 | 无脑调大 redo |
| 行锁等待高 | 缩短事务/改索引/降冲突 | 隔离级别评估 | 加线程 |
| `Created_tmp_disk_tables` 高 | 索引/改写 SQL | 增大 tmpdir 空间 | 只调 sort_buffer |
| MDL 等待 | 避免长事务与 DDL 撞车 | 在线 DDL 工具 | 强杀 DDL |
| `futex` 高、IPC 低 | 减少共享写/绑核 | 线程池 | 加 worker |
| 远端 NUMA 高 | `numactl --membind` | 分实例 | 跨片部署 |

## 7. 采样与分析清单

- 采样前后状态文件齐全，时间窗口明确。
- 速率已按秒归一化，累计值未直接比较。
- PFS/INNODB_METRICS 开关在两腿一致。
- Topdown 环境（物理机/虚拟机/容器）已注明。
- 结论至少有一个独立证据（计数器/对照实验/火焰图）支撑。
- 负结果与未解释现象已记录。
