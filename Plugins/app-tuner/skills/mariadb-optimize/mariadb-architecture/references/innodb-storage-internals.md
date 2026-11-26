# InnoDB 存储内部结构

本文件是热点分析的概念底座：buffer pool、B+tree、redo/undo、MVCC、行锁与 latch、
doublewrite、adaptive hash、purge 与 page cleaner。路径以 10.11.16 为准；
change buffer 在 10.11 默认 `innodb_change_buffering=none`；参数 10.9 起弃用（设置仍生效并告警），11.0 移除。

## 1. 内存结构

| 结构 | 位置 | 作用 | 竞争点 |
| --- | --- | --- | --- |
| buffer pool | `buf/buf0buf.cc`、`buf0lru.cc` | 缓存数据/索引/undo/系统页 | 页 hash、LRU 链表、free list |
| log buffer | `log/log0log.cc` | 暂存 redo，批量写日志文件 | `log_sys` latch、写入顺序 |
| change buffer（10.11 默认 none） | `ibuf/ibuf0ibuf.cc` | 缓存非唯一二级索引写；`innodb_change_buffering` 在 10.11.16 仍存在且生效（默认 none，10.9 起弃用告警，11.0 移除） | ibuf bitmap 页、合并 |
| adaptive hash | `btr/btr0sea.cc` | 等值查找的哈希索引 | AHI latch、分区 |
| lock system | `lock/lock0lock.cc` | 行锁/表锁/意向锁 | `lock_sys` latch、hash 桶 |
| trx system | `trx/trx0trx.cc` | 事务对象、read view、历史列表 | `trx_sys` latch |
| doublewrite | `buf/buf0dblwr.cc` | 部分页写保护 | 双写缓冲与刷盘顺序 |

### buffer pool 要点

- 以页（默认 16KB）为单位，容量由 `innodb_buffer_pool_size` 控制；10.11 已移除
`innodb_buffer_pool_instances`，buffer pool 内部自行分片，不再由该参数调节争用。
- 页查找是 hash + 链表移动；高并发下 LRU 与 hash 桶可能成为热点。
- 未命中触发同步读；连续访问触发线性/随机预读（`innodb_read_ahead_threshold`）。
- `innodb_buffer_pool_dump_at_shutdown` / `load_at_startup` 决定重启后预热；
压测/恢复场景常关闭以避免额外 IO 干扰。
- 10.11 支持在线 resize；缩容会触发页回收，期间性能可能抖动。

## 2. B+tree 与记录

- 聚簇索引叶子 = 数据页；非叶子 = 键 + 子页号。
- 二级索引叶子 = 索引列 + 主键；查询非覆盖列需回表。
- 页内记录按主键有序，页分裂/合并由 `btr/btr0btr.cc`、`btr0cur.cc` 处理。
- 记录格式 `DYNAMIC` 时大字段存 off-page，主记录保留 20 字节指针。
- 前缀索引、联合索引的最左前缀决定 range 能否使用；索引选择性差会导致大量回表。

## 3. redo log

- 物理逻辑日志：记录“对某页某偏移做了什么”，用于崩溃恢复。
- 写入顺序：事务修改 → log buffer → （按 `innodb_flush_log_at_trx_commit`）刷 log file。
- 容量即 `innodb_log_file_size`（默认 96MB，动态）；10.11 为单 `ib_logfile0`、支持在线调整。
- checkpoint 推进决定可复用的日志空间；刷脏跟不上会导致 checkpoint 等待/写入抖动。
- `log_sys` 是全局 latch 点，高并发写入时常见热点；组提交减少 fsync 次数但不消除
log buffer 拷贝与顺序化。

## 4. undo、MVCC 与 purge

- 修改前镜像写入 undo；回滚与一致性读都依赖 undo。
- read view 决定哪些事务 ID 可见；RC 每条语句一个 read view，RR 事务内复用。
- 历史版本链沿 `DB_ROLL_PTR` 回溯；链过长会增加读放大与 CPU。
- purge 线程清理不再需要的 undo；长事务/长查询持有旧 read view 会阻塞 purge，
导致 undo 表空间膨胀、`History list length` 增大。
- 监控：`SHOW ENGINE INNODB STATUS` 的 `History list length`、
`INFORMATION_SCHEMA.INNODB_METRICS` 的 `trx_rseg_history_len`。

## 5. 锁与 latch（务必区分）

### 5.1 事务锁（用户可见）

| 锁 | 粒度 | 触发 |
| --- | --- | --- |
| 记录锁 | 行 | 唯一索引等值更新/删除 |
| 间隙锁 | 区间 | RR 下范围条件、防幻读 |
| next-key | 行 + 间隙 | RR 默认组合 |
| 插入意向 | 间隙 | 并发插入同一间隙 |
| 表意向 IS/IX | 表 | DML 自动加 |
| AUTO-INC | 表/轻量 | 自增分配 |
| MDL | 元数据 | DDL/DML 互斥、显式 LOCK TABLES |

诊断：`INFORMATION_SCHEMA.INNODB_TRX`、`INNODB_LOCK_WAITS`、
`performance_schema.metadata_locks`（MDL；`data_locks` 是 MySQL 8.0 表，10.11 无）、
`SHOW ENGINE INNODB STATUS` 的
`TRANSACTIONS`/`LATEST DETECTED DEADLOCK`。

### 5.2 latch（内部短临界区，用户不可见）

| 类型 | 头文件 | 语义 | 10.11 实现 |
| --- | --- | --- | --- |
| `rw_lock` | `include/rw_lock.h` | 读写 latch，单一 `std::atomic<uint32_t>` | 10.6 起重写；无 `lock_word` 字段 |
| `srw_lock` | `include/srw_lock.h` | 共享/排他，futex 或 SRWLOCK | `sync/srw_lock.cc` |
| `sux_lock` | `include/sux_lock.h` | S/U/X 三态，支持递归 U/X | 基于 `srw_lock` |
| mutex | `sync/` / `pthread_mutex` | 短临界区 | 视平台 |

> 历史提示：10.3 的 `rw_lock_t` 用 `lock_word` 编码读写状态，案例中的
> “读写锁改顺序锁”“拆分 lock_word 到 s_lock_word”针对该实现。10.11 已是
> `std::atomic` 的 `rw_lock`，这些字段不存在；移植前必须重新抓热点。

## 6. doublewrite、AHI 与 change buffer

- **doublewrite**：脏页先写双写区再写数据文件，防止页写一半导致无法恢复。
10.11 双写区在系统表空间中，由 `innodb_doublewrite` 控制；`innodb_doublewrite_dir`
是 MySQL 8.0 参数，10.11 无此选项。写密集场景有额外 IO。
- **change buffer（10.11 默认关闭）**：非唯一二级索引的插入/更新可先记在 ibuf、后续合并，
减少随机读，但合并时有额外 IO 与 latch 竞争，由 `innodb_change_buffering` 控制。
**10.11.16 该参数仍存在且设置仍生效，默认 `none`（10.9 起标记弃用并告警，11.0 才移除）**：
不要把随机 IO 或 CPU 热点归因到它，先 `SHOW VARIABLES LIKE 'innodb_change_buffering'` 确认值为
`none`；历史版本案例移植前必须先确认版本。
- **adaptive hash index**：对高频等值查找建立哈希，减少 B+tree 层次；
`innodb_adaptive_hash_index` 可关闭以降低 latch 竞争（需实测）。

## 7. 刷脏与后台线程

| 线程 | 作用 | 相关参数 |
| --- | --- | --- |
| page cleaner | 刷脏页、推进 checkpoint | `innodb_io_capacity`（10.11 已移除 `innodb_page_cleaners`） |
| log writer/flusher | 写/刷 redo | `innodb_flush_log_at_trx_commit`、`innodb_log_write_ahead_size` |
| purge | 清理 undo | `innodb_purge_threads`、`innodb_max_purge_lag` |
| read/write IO | 异步 IO 完成 | `innodb_read_io_threads`、`innodb_write_io_threads`、`innodb_use_native_aio` |
| buf dump/load | 重启预热 | `innodb_buffer_pool_dump_at_shutdown` |

刷脏能力不足的典型证据：`Innodb_buffer_pool_pages_dirty` 高、
`Innodb_log_waits` 增长、写入 P99 抖动、`SHOW ENGINE INNODB STATUS` 出现
`pending writes`/checkpoint 等待。

## 8. 诊断入口速查

```
SHOW ENGINE INNODB STATUS\G
SELECT * FROM INFORMATION_SCHEMA.INNODB_TRX;
SELECT * FROM INFORMATION_SCHEMA.INNODB_LOCK_WAITS;
SELECT NAME, COUNT, ENABLED FROM INFORMATION_SCHEMA.INNODB_METRICS
 WHERE NAME IN ('trx_rseg_history_len','lock_row_lock_waits','buffer_pool_reads');
-- 10.11 的 ENABLED 是数值列（1=已启用，0=未启用），不是字符串 STATUS；
-- 筛已启用项用 WHERE ENABLED=1，旧版若确有 STATUS 列再按该版本分支处理。
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool%';
SHOW GLOBAL STATUS LIKE 'Innodb_log%';
```

> `INNODB_METRICS` 默认多数计数器未启用，需要 `SET GLOBAL innodb_monitor_enable='module'`
> 或指定计数器；采集前确认，避免“看不到数据”被误判为“没有发生”。
