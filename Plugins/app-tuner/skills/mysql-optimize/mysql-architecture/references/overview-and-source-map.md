# 总览、内存结构与源码地图

>
> 版本基线：MySQL 8.0.25 / 8.0.28。线程名与参数默认值以 `performance_schema.threads` 与
> `sql/sys_vars.cc` 为准；不同小版本可能增减后台线程。

## 1. 分层职责

| 层 | 入口 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 连接层 | 监听 socket、`handle_connection` | 认证、会话、命令循环 | 不解析业务语义 |
| SQL 层 | `dispatch_command` → parser/optimizer/executor | 语义、权限、执行计划、表达式、排序、临时表 | 不直接读写页 |
| handler | `handler::ha_*` / `ha_innobase` | 行/索引访问抽象、事务边界、锁调用 | 不决定执行计划 |
| InnoDB | `row_search_mvcc`、`btr_*`、`buf_*`、`log_*`、`trx_*` | 页、记录、事务、MVCC、锁、redo/undo、刷脏 | 不解析 SQL |
| 复制 | `Binlog_sender`、applier | 日志发送/应用、组提交 | 不参与本地读路径 |

## 2. 关键内存结构

| 结构 | 位置 | 说明 | 调优关联 |
| --- | --- | --- | --- |
| Buffer Pool | `buf_pool_t` | 数据页缓存，按 instance 分片，页哈希 + LRU + flush list | `innodb_buffer_pool_size/instances/chunk_size` |
| Redo log buffer | `log_sys` | 事务产生的 redo 先入内存，再按策略刷盘 | `innodb_log_buffer_size` |
| Redo 文件 | `ib_logfile*` / `#innodb_redo` | 环形写，checkpoint 推进可覆盖 | `innodb_log_file_size`、`innodb_log_files_in_group` |
| Undo | undo tablespace | 旧版本行、回滚、MVCC 读 | `innodb_undo_tablespaces`、purge 线程 |
| Change Buffer | `ibuf` | 二级索引非唯一页的延迟合并 | `innodb_change_buffer_max_size/buffering` |
| AHI | `btr_search` | 索引前缀→页的哈希加速 | `innodb_adaptive_hash_index` |
| 锁系统 | `lock_sys` | 行锁、表锁、等待队列、死锁检测 | 隔离级别、索引设计、`innodb_lock_wait_timeout` |
| 事务系统 | `trx_sys` | 活跃事务、Read View 列表、序列号 | `trx_sys->mutex` 竞争、Read View 复用 |
| 表缓存 | `table_open_cache` | 打开表对象缓存 | `table_open_cache/instances` |
| 预备语句 | `Prepared_statement` | PS 元数据与执行计划 | `max_prepared_stmt_count` |

## 3. 后台线程与可观测名字

`performance_schema.threads.NAME` 由 PSI 注册键名 `m_name` 拼成 `thread/<category>/<m_name>`，
InnoDB 线程因此是 `thread/innodb/<键名>_thread`；`ib_*` 是另一路 `m_os_name`，只写 OS
线程名，两层不可混用。

| 线程（`performance_schema.threads.NAME`） | OS 线程名（`comm`，8.0.27+） | 作用 | 常见热点 |
| --- | --- | --- | --- |
| `thread/sql/one_connection` | `connection`（8.0.28+） | 业务连接（`handle_connection`） | 解析、执行、页查找、锁等待 |
| `thread/innodb/log_writer_thread` | `ib_log_writer` | 把 log buffer 写到 redo 文件 | `log_write_up_to`、IO |
| `thread/innodb/log_flusher_thread` | `ib_log_flush` | redo fsync | `fsync`、提交延迟 |
| `thread/innodb/log_checkpointer_thread` | `ib_log_checkpt` | checkpoint 推进 | redo 空间、刷脏 |
| `thread/innodb/page_flush_thread` / `page_flush_coordinator_thread` | `ib_pg_flush`（以实例查询为准） | 刷脏页（page cleaner，可多个） | `buf_flush_*`、IO、LRU |
| `thread/innodb/srv_purge_thread` | `ib_srv_purge` | 清理 undo（purge coordinator/worker） | 长事务、历史版本链 |
| `thread/innodb/dict_stats_thread` | `ib_dict_stats` | 统计信息 | 统计采样 |
| `thread/innodb/buf_dump_thread` | `ib_buf_dump` | buffer pool dump | 启动/关闭 |
| `thread/innodb/srv_master_thread` | `ib_src_main`（8.0.28） | purge 等杂务；8.0 起不再做周期 checkpoint（移交 log checkpointer） | 杂务、purge |
| `thread/sql/event_scheduler` | — | 事件调度 | — |
| `thread/sql/compress_gtid_table` | — | GTID 压缩 | — |

两版注册机制：8.0.25 `ha_innodb.cc:627` 的 `PSI_KEY(n,...) { &n##_key, #n, ... }` 与
8.0.28 `ha_innodb.cc:635` 的 `PSI_THREAD_KEY(n, osn, ...) { &n##_key, #n, osn, ... }`
都以 `#n` 为 `m_name`（故 NAME 带 `_thread` 后缀），`osn` 只进 `m_os_name`；`pfs.cc`
用 `m_os_name` 调 `my_thread_self_setname`（`comm`，15 字符截断）。8.0.25/8.0.26 未为
InnoDB 线程设 OS 名，`ps -T`/`top -H` 的 `comm` 显示 `mysqld`；8.0.28 起且
`performance_schema=ON` 时才出现上表 OS 名。

MySQL 8.0 **仍有**独立 master thread（`thread/innodb/srv_master_thread`），只是周期
checkpoint 从它移交 log checkpointer；不要写「已无 master thread」。

识别方法：`SELECT THREAD_ID, NAME, THREAD_OS_ID, PROCESSLIST_ID FROM performance_schema.threads;`
（`THREAD_OS_ID` 即 `/proc/<pid>/task` 下 TID，可用于绑核）；或用 `pstack`/`gdb` 看栈关键字。

## 4. 端口、目录与文件

| 项 | 默认/常见 | 说明 |
| --- | --- | --- |
| TCP 端口 | 3306（MySQL X 33060） | `port`、`mysqlx_port` |
| unix socket | `${DATA_ROOT}/mysql.sock` | 本机压测可绕过 TCP |
| 数据目录 | `${DATA_ROOT}` | `datadir` |
| 系统表空间 | `ibdata1` | 默认 12M 自动扩展 |
| 独立表空间 | `*.ibd` | `innodb_file_per_table` |
| redo | `ib_logfile0..N` 或 `#innodb_redo/` | 8.0.30+ 改为固定数量文件 |
| undo | `undo_001`、`undo_002` | 8.0 默认两个 undo 表空间 |
| binlog | `mysql-bin.000001` | `log_bin`、`max_binlog_size` |
| relay log | `relay-log.*` | 复制 |
| 错误日志 | `${DATA_ROOT}/mysql.log` 或 `log-error` | 启动/崩溃/告警 |
| 慢日志 | `slow_query.log` | `slow_query_log`、`long_query_time` |

## 5. 版本差异与注意点

- 8.0.30 起 redo 组织为 `#innodb_redo` 下的固定文件集合，`innodb_log_files_in_group`
语义变化；8.0.25/8.0.28 仍是 `ib_logfile*` + `innodb_log_file_size`。
- 8.0 默认 `performance_schema=ON`；压测常关闭以减少开销，但会失去等待/线程证据。
- 8.0 默认 `binlog_format=ROW`、`binlog_row_image=FULL`；写放大与 binlog 体积相关。
- Percona Server 5.7/8.0 增加 thread pool、extra port、`innodb_buffer_pool_hot_cache`
等；参数名以 `SHOW VARIABLES` 为准，不要假设与 Oracle MySQL 完全一致。
- 容器/虚机中的“CPU 数”是 cpuset/vCPU 视角，NUMA 与中断归属仍由宿主机决定。

## 6. 源码阅读顺序（建议）

1. `sql/sql_parse.cc`：`dispatch_command` 如何分流 SELECT/INSERT/UPDATE。
2. `sql/sql_select.cc` / `sql_optimizer.cc`：计划如何产生（join order、索引选择）。
3. `sql/sql_executor.cc` + `sql/iterators/`：迭代器如何驱动 handler。
4. `sql/handler.cc` 与 `storage/innobase/handler/ha_innodb.cc`：Server→InnoDB 边界。
5. `storage/innobase/row/row0sel.cc`：行读取与锁判断。
6. `btr0cur.cc` / `page0cur.cc` / `rem/rec.cc`：页内查找与记录 offset。
7. `read/read0read.cc` + `trx/trx0trx.cc`：MVCC 与提交。
8. `log/log0log.cc` + `sql/binlog.cc` + `rpl_commit_stage_manager.cc`：两阶段与组提交。
9. `strings/ctype-utf8.cc`：排序键与哈希（ORDER BY/DISTINCT 热点根源）。

## 7. 快速核对命令（只读）

```
SELECT VERSION();
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'innodb_log_file_size';
SHOW VARIABLES LIKE 'innodb_flush_log_at_trx_commit';
SHOW VARIABLES LIKE 'sync_binlog';
SHOW VARIABLES LIKE 'innodb_adaptive_hash_index';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read_requests';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_reads';
SHOW ENGINE INNODB STATUS\G
```
