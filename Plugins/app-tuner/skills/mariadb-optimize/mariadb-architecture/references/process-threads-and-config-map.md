# 进程/线程模型、文件布局与参数地图

本文件回答“MariaDB 运行时有哪些线程、数据落在哪些文件、端口/socket 如何规划、
常用参数影响什么”。默认值以 10.11.16 源码声明为准，易漂移项标“源码默认”。

## 1. 进程模型

MariaDB 默认是**单进程多线程**：一个 `mariadbd`（或 `mysqld`）进程，内含所有连接线程与
后台线程。多实例部署 = 多个进程，各自独立端口/socket/数据目录。

| 部署形态 | 进程 | 适用 | 注意 |
| --- | --- | --- | --- |
| 单实例 | 1 × mariadbd | 常规 OLTP | 最简；绑核按 NUMA 规划 |
| 多实例 | N × mariadbd | 隔离租户、提升资源利用率 | 端口/socket/内存/绑核必须成组规划 |
| 主从 | 主 + 从（各自单/多实例） | 读写分离、容灾 | 复制延迟、GTID、半同步 |
| Galera | 3+ 节点 | 同步多主/高可用 | 写集认证延迟、网络质量 |

## 2. 线程模型

### 2.1 连接线程

- `thread_handling=one-thread-per-connection`（默认）：每连接一个线程，
受 `max_connections` 限制；线程缓存 `thread_cache_size` 复用已退出线程。
- `thread_handling=pool-of-threads`：固定 worker 池，事件驱动；
相关参数 `thread_pool_size`（通常≈CPU 核数）、`thread_pool_max_threads`、
`thread_pool_stall_limit`（单位毫秒 ms）、`thread_pool_priority`（高低优先级队列）。

选择：

- 高并发、短查询 → 线程池通常更稳（减少线程创建与上下文切换）。
- 少量长查询/大事务 → 线程池可能被占满，但**不要直接提高 stall limit**：
 `thread_pool_stall_limit` 单位是毫秒，表示停滞检查的间隔。值越大检查越稀疏、
 越晚把长查询判为 stalled 并补充 worker，短请求排队时反而更晚获得 worker。
 调整前后要对比排队时间与停滞观测（`INFORMATION_SCHEMA.THREAD_POOL_QUEUES` /
 `THREAD_POOL_STATS` 的队列与 stall 计数、请求排队耗时）：排队变长且 stall 变少说明
 调整反而恶化，应回退，或改用每连接线程/按优先级队列分流长查询。

### 2.2 后台线程（名称随版本略有差异，用 `ps -L` 或 performance_schema 核对）

| 线程 | 作用 |
| --- | --- |
| page cleaner（线程名随版本） | 刷脏页、推进 checkpoint |
| `innodb_log_writer`/`log_flusher` | 写/刷 redo |
| `innodb_purge_*` | 清理 undo、回滚段 |
| `innodb_io_read/write` | 异步 IO 完成 |
| `innodb_buf_dump/load` | 重启预热 |
| `aria_pagecache`/checkpoint | Aria 页缓存与检查点 |
| `Binlog dump` | 主库向从库发送 binlog |
| `Slave_IO`/`Slave_SQL`/worker | 从库接收与回放 |
| `event_scheduler` | 定时事件 |
| `wsrep_*` | Galera 复制 |

线程命名不是稳定 API；**按 PID + 线程名 + CPU/等待状态**联合判断，不要只 grep 名字。

## 3. 文件布局

| 文件/目录 | 内容 | 关键参数 |
| --- | --- | --- |
| `ibdata1` | 系统表空间（含部分 undo/字典） | `innodb_data_file_path` |
| `*.ibd` | 每表文件表空间 | `innodb_file_per_table` |
| `ibtmp1` | 临时表空间 | `innodb_temp_data_file_path` |
| `ib_logfile0`（10.8 起单文件、可在线 resize；数据目录中的 `ib_logfile1` 属 ≤10.7 升级遗留，无 MySQL 8.0 的 `#innodb_redo/`） | redo | `innodb_log_file_size`、`innodb_log_group_home_dir` |
| 系统表空间 `ibdata1` 内的双写区 | 双写缓冲 | `innodb_doublewrite`（10.11 无 `innodb_doublewrite_dir`） |
| `aria_log*` | Aria 日志 | `aria_log_file_size`、`aria_log_dir_path` |
| `*-bin.00000*`（默认 hostname 或 log-basename 加 `-bin`；MySQL 8.0 才是 `binlog.000001`） | binlog | `log_bin`、`max_binlog_size`、`binlog_format` |
| `*-relay-bin.*`（默认 hostname 或 log-basename 加 `-relay-bin`） | 从库中继日志 | `relay_log`、`relay_log_purge` |
| `*-slow.log` | 慢日志 | `slow_query_log`、`long_query_time` |
| `#sql-*.frm/ibd` | 在线 DDL 临时表 | `innodb_online_alter_log_max_size` |
| `mysql/`、`performance_schema/` | 系统库 | — |

规划原则：

- redo、binlog、数据文件分盘可降低写竞争；NVMe 上更看重队列深度与 `innodb_io_capacity`。
- `innodb_log_group_home_dir` 与 `innodb_data_home_dir` 分离便于独立扩容。
- 数据盘建议 XFS + `noatime`；`O_DIRECT` 下页缓存交给 buffer pool 管理。

## 4. 端口与 socket

| 用途 | 默认 | 参数 |
| --- | --- | --- |
| MySQL 协议 | 3306 | `port` |
| unix socket | 编译期/配置 | `socket` |
| 管理/额外端口 | 关闭 | `extra_port`（连接满时救援） |
| Galera | 4567/4568/4444 | `wsrep_*` |
| 备份/监控 | 视工具 | — |

多实例：每个实例独立 `port`、`socket`、`pid-file`、`datadir`、`log-error`、
`tmpdir`，并分别绑核；不要共用 socket 或 pid 文件。

## 5. 参数地图（源码默认，生产需按负载重设）

### 5.1 连接与线程

| 参数 | 源码默认 | 影响 |
| --- | --- | --- |
| `max_connections` | 151 | 并发连接上限；每连接占内存 |
| `thread_cache_size` | 自动 | 复用线程，减少创建开销 |
| `thread_handling` | one-thread-per-connection | 线程模型 |
| `thread_pool_size` | CPU 相关 | 线程池 worker 数 |
| `table_open_cache` | 2000（量级） | 打开表缓存；不足则频繁开关表 |
| `table_definition_cache` | 自动 | 表定义缓存 |

### 5.2 InnoDB 内存与 IO

| 参数 | 源码默认 | 影响 |
| --- | --- | --- |
| `innodb_buffer_pool_size` | 128MB | 页缓存容量，OLTP 首要参数 |
| `innodb_log_file_size` | 96MB | redo 容量，影响 checkpoint 与写吞吐 |
| `innodb_flush_method` | `O_DIRECT` | 绕过 page cache（MariaDB 默认） |
| `innodb_io_capacity` | 200 | 后台 IO 速率 |
| `innodb_io_capacity_max` | 自动 | 峰值 IO 上限 |
| `innodb_flush_log_at_trx_commit` | 1 | 提交持久性 vs 延迟 |
| `innodb_doublewrite` | ON | 部分页写保护 |
| `innodb_use_native_aio` | ON（Linux） | 异步 IO |
| `innodb_read_io_threads` / `write_io_threads` | 4 | IO 并发 |
| `innodb_numa_interleave` | OFF | 大页/NUMA 内存交错 |

> 10.11 已移除 `innodb_buffer_pool_instances`、`innodb_page_cleaners` 与
> `innodb_thread_concurrency`：写进 my.cnf 会报 unknown variable（见排障表首行）。
> buffer pool 内部自行分片，page cleaner 线程数由版本管理，进入 InnoDB 的并发不再由参数设上限。

### 5.3 日志、复制与优化器

| 参数 | 源码默认 | 影响 |
| --- | --- | --- |
| `sync_binlog` | 0（MariaDB 10.11 源码默认，由 OS 安排刷盘） | binlog 刷盘；与 redo 一起决定持久性；用 `SELECT @@GLOBAL.sync_binlog` 核对实例实际值 |
| `binlog_format` | `MIXED` | 复制语义与体积 |
| `binlog_commit_wait_count` | 0（不等待） | 组提交等待的事务数；为 0 时组提交不额外等待 |
| `binlog_commit_wait_usec` | 100000（微秒，约 100ms） | 等待时限，仅在 `binlog_commit_wait_count > 0` 时生效；只调大 count 而沿用默认 usec，低并发提交会等到时限，可能额外等待约 100ms |
| `gtid_strict_mode` | OFF | GTID 严格性 |
| `slave_parallel_threads` | 0 | 从库并行回放 |
| `optimizer_switch` | 版本相关 | 优化器行为开关 |
| `performance_schema` | OFF（MariaDB 默认） | 采集开销与可观测性 |
| `slow_query_log` | OFF | 慢日志 |
| `long_query_time` | 10 | 慢日志阈值 |

### 5.4 Aria

| 参数 | 影响 |
| --- | --- |
| `aria_pagecache_buffer_size` | Aria 页缓存，内部临时表多时重要 |
| `aria_log_file_size` | Aria redo |
| `aria_pagecache_age_threshold` | LRU 老化 |
| `aria_page_checksum` | 页校验开销 |

## 6. 可观测性入口

```
SHOW VARIABLES LIKE 'version%';
SHOW VARIABLES LIKE 'performance_schema';
SHOW GLOBAL STATUS LIKE 'Threads_%';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool%';
SHOW GLOBAL STATUS LIKE 'Handler_%';
SHOW GLOBAL STATUS LIKE 'Created_tmp%';
SELECT * FROM performance_schema.threads;              -- 需开启 PFS
SELECT * FROM performance_schema.events_waits_current; -- 需开启 PFS
```

注意：

- MariaDB 的 `performance_schema` 默认关闭；开启会带来内存与少量 CPU 开销，
压测对比必须两腿一致（都开或都关）。
- `SHOW GLOBAL STATUS` 是累计值；采样要取差值并按时间归一化。
- 线程/连接级问题优先看 `PROCESSLIST` + `INNODB_TRX` + 等待事件，而不是只看 QPS。

## 7. 输出要求

部署/架构类回答必须给出：进程数、线程模型、端口/socket、数据/日志目录、
NUMA/绑核假设、关键参数取值与来源（默认/实测/显式配置），以及如何验证。
