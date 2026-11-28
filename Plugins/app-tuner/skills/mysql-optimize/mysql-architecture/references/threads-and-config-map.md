# 线程模型与配置参数地图

## 1. 线程模型

### 1.1 连接线程

- 默认 `one-thread-per-connection`：每连接一个 `handle_connection` 线程。
- 并发数 ≈ 线程数；`thread_cache_size` 缓存空闲线程，降低创建开销。
- 长连接 + 连接池可减少建连；短连接高并发会放大线程创建/销毁与认证成本。
- Percona thread pool：`thread_pool_size`、`thread_pool_oversubscribe`、
`thread_pool_stall_limit`；适合大量短查询，但需按版本确认参数名。

### 1.2 后台线程

| 线程（`performance_schema.threads.NAME`） | 何时活跃 | 绑核建议（读/写） |
| --- | --- | --- |
| `thread/innodb/log_writer_thread` | 写事务提交、redo 写盘 | 写场景独立核；读场景可与其他后台共享 |
| `thread/innodb/log_flusher_thread` | fsync redo | 写场景独立核（与 writer 分开） |
| `thread/innodb/log_checkpointer_thread` | checkpoint 推进 | 通常无需单独绑核 |
| `thread/innodb/page_flush_thread` / `page_flush_coordinator_thread` | 脏页比例/checkpoint（page cleaner） | 写场景关注；多实例时按 instance |
| `thread/innodb/srv_purge_thread` / purge worker | 有旧版本需清理 | 长事务多时关注 |
| `thread/innodb/dict_stats_thread` | 统计信息更新 | 低优先级 |
| `thread/innodb/buf_dump_thread` | 启停 | 不影响稳态 |
| `thread/innodb/srv_master_thread` | purge 等杂务（8.0 仍存在，但不再做周期 checkpoint） | 通常无需单独绑核 |

识别（按 `threads.NAME` 查，`ib_*` 是 OS 线程名，不能用来过滤 NAME；后台线程名逐版不同，
以实例查询为准）：

```
SELECT THREAD_ID, NAME, TYPE, PROCESSLIST_ID, THREAD_OS_ID
FROM performance_schema.threads
WHERE NAME LIKE 'thread/innodb/log%'
   OR NAME LIKE 'thread/innodb/page_flush%'
   OR NAME LIKE 'thread/innodb/%purge%'
   OR NAME LIKE 'thread/innodb/dict_stats%'
   OR NAME LIKE 'thread/innodb/buf_dump%'
   OR NAME = 'thread/innodb/srv_master_thread';
```

`NAME` 由 PSI 注册键名生成，8.0.25/8.0.28 都是 `..._thread` 后缀；`ib_*` 只进
`m_os_name`，经 `my_thread_self_setname` 写成 OS 线程名（`ps -T`/`top -H` 的 `comm`，15
字符截断，8.0.27 起才有；8.0.25/8.0.26 显示 `mysqld`），8.0.28 的 flusher/checkpointer
OS 名是 `ib_log_flush`/`ib_log_checkpt`。`THREAD_OS_ID` 即 `/proc/<pid>/task` 下的 TID，
可直接用于 `taskset -pc`；`performance_schema=OFF` 时查不到这些线程。

或用 `pstack $(pidof mysqld) | grep -E 'log_writer|log_flusher|handle_connection'`。

### 1.3 线程与 CPU 的对应

- 只读高 QPS：业务线程数 ≈ 并发；网络中断需与业务核分离。
- 读写混合：log 线程与 page cleaner 会周期性抢占；单独绑核可降尾延迟，但核少时
会减少业务可用核，需 A/B。
- 容器/虚机：guest 看到的 CPU 是 cpuset/vCPU；host 中断与 steal time 仍会影响。

## 2. 配置参数地图

### 2.1 连接与元数据

| 参数 | 作用 | 场景建议 |
| --- | --- | --- |
| `max_connections` | 最大连接 | ≥ 压测并发，留余量 |
| `back_log` | accept 队列 | 高并发建连时增大 |
| `thread_cache_size` | 空闲线程缓存 | 接近并发峰值 |
| `table_open_cache` | 打开表缓存 | 表多/并发高时增大 |
| `table_open_cache_instances` | 表缓存分片 | 高并发降争用 |
| `max_prepared_stmt_count` | PS 上限 | sysbench `db-ps-mode=auto` 时按 并发×语句数 估算 |
| `open_files_limit` | 文件描述符 | 随表数/连接数增大 |

### 2.2 内存与缓存

| 参数 | 作用 | 备注 |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 数据页缓存 | 通常内存 50%–70% |
| `innodb_buffer_pool_instances` | 分片数 | 大 pool 才需要多个 |
| `innodb_buffer_pool_chunk_size` | 分配粒度 | 影响在线调整 |
| `innodb_log_buffer_size` | redo 内存缓冲 | 大事务/高并发可增大 |
| `innodb_change_buffer_max_size` | change buffer 占比 | 写多读少可增大 |
| `innodb_adaptive_hash_index` | AHI | 场景化，见下 |
| `performance_schema` | 观测开销 | 压测常 OFF，但会失去等待证据 |

### 2.3 redo / binlog / 刷盘

| 参数 | 作用 | 备注 |
| --- | --- | --- |
| `innodb_log_file_size` | 单个 redo 文件大小 | 大文件减少 checkpoint 频率，恢复变慢 |
| `innodb_log_files_in_group` | redo 文件数 | 8.0.30 前有效 |
| `innodb_flush_log_at_trx_commit` | 提交 redo 策略 | 1/2/0，权衡 durability |
| `sync_binlog` | binlog fsync 频率 | 1/N/0；N 按提交组计数，不是事务数——同组事务共享一次 fsync |
| `innodb_flush_method` | 文件 IO 方式 | `O_DIRECT` 常用 |
| `innodb_io_capacity(_max)` | 后台 IO 速率 | 按盘能力设置 |
| `innodb_max_dirty_pages_pct(_lwm)` | 脏页水位 | 高写入可调高，但恢复/尾延迟风险 |
| `innodb_lru_scan_depth` | LRU 扫描深度 | 与 page cleaner 配合 |
| `innodb_page_cleaners` | 刷脏线程 | 大 pool/多盘可增 |
| `innodb_flush_neighbors` | 邻近页刷 | SSD 常 0 |
| `innodb_doublewrite` | 页原子性 | NVMe 原子写确认后可关 |

### 2.4 并发与自旋

| 参数 | 作用 | 备注 |
| --- | --- | --- |
| `innodb_spin_wait_delay` | 自旋间隔 | `ut_delay` 热点时调整 |
| `innodb_sync_spin_loops` | 自旋次数 | 与上者联合调 |
| `innodb_thread_concurrency` | 并发线程上限 | 默认 0（不限） |
| `innodb_concurrency_tickets` | 进入内核的票据 | 高并发可调 |
| `innodb_read_io_threads` | 读 IO 线程 | 只读可增 |
| `innodb_write_io_threads` | 写 IO 线程 | 只读可降，写多可增 |
| `innodb_purge_threads` | purge 线程 | 长事务/写多可增 |

### 2.5 复制与日志

| 参数 | 作用 |
| --- | --- |
| `log_bin` / `binlog_format` | 是否开 binlog、格式 |
| `max_binlog_size` | rotate 阈值 |
| `binlog_row_image` | 行镜像（FULL/MINIMAL） |
| `binlog_order_commits` | 保证提交顺序 |
| `binlog_group_commit_sync_delay` | 组提交延迟等待 |
| `binlog_group_commit_sync_no_delay_count` | 组提交批量阈值 |
| `relay_log` / `slave_parallel_workers` | 复制应用 |

## 3. 在线 / 重启 / 重编译分类

| 类别 | 例子 | 变更方式 |
| --- | --- | --- |
| 动态全局 | `max_connections`、`innodb_io_capacity`、`innodb_spin_wait_delay` | `SET GLOBAL`，注意会话/全局 |
| 动态但需谨慎 | `innodb_buffer_pool_size`（chunk 对齐）、`innodb_adaptive_hash_index` | 在线调整可能抖动 |
| 需重启 | `innodb_log_file_size`、`innodb_buffer_pool_instances`（部分版本）、`innodb_doublewrite` | 改配置 + 重启 |
| 仅初始化可设 | `innodb_page_size` | 只能在新建实例初始化时确定；对已有实例改值后重启会因与现有表空间页大小不匹配而无法启动。变更需新建实例并迁移数据，不属于“重启生效项” |
| 需重编译 | `-march`、LTO、源码补丁、PSI 开关 | 重新构建并替换二进制 |

## 4. AHI 决策（与上层 database-workload-analysis 对齐）

- 推荐 OFF：`mysqld_cpu_pct > 90` 且 `threads_per_core > 4` 且
`buffer_pool_hit_rate > 95` 且负载为只读/读多。
- 保持 ON：`mysqld_cpu_pct < 70` 或 `buffer_pool_hit_rate < 90` 或
`threads_per_core <= 2`。
- 中间区间：`need_more_evidence`，纳入下一轮串行 A/B。
- 证据：`SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%'` 计算命中率；
`performance_schema` 或 `pidstat` 取 CPU；并发/核数算线程密度。

## 5. 配置变更影响面速查

| 改动 | 影响 | 必须验证 |
| --- | --- | --- |
| buffer pool 大小/实例 | 内存、命中率、页 latch | 命中率、P95、OOM |
| redo 文件/缓冲 | 恢复时间、checkpoint、写尾延迟 | 写 TPS/P99、崩溃恢复 |
| flush/sync 策略 | durability、fsync 频率 | 数据安全声明、写性能 |
| AHI | CPU、latch、随机读 | 只读 A/B、命中率 |
| 线程数/绑核 | CPU 分布、上下文切换 | `mpstat`、热点迁移 |
| binlog 参数 | 复制、磁盘、写放大 | 主从一致性、binlog 大小 |
| 字符集/collation | 排序语义、索引长度 | 结果正确性、MTR |
