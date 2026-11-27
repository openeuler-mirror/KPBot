# InnoDB 锁、latch 与线程模型（热点解释基础）

本文件解释 MariaDB 热点里最常见的“争用”从哪来：事务锁、MDL、内部 latch、
线程角色与版本演进。理解这些，才能把火焰图上的 `futex`/原子操作映射到具体机制。

## 1. 三层“锁”

| 层 | 对象 | 用户可见 | 典型等待 |
| --- | --- | --- | --- |
| 事务锁 | 行/间隙/表意向/自增 | 是（`INNODB_TRX`/`LOCK_WAITS`） | 行锁等待、死锁 |
| 元数据锁 MDL | 表/库/全局 | 部分（`SHOW PROCESSLIST` 状态） | DDL 与 DML 互斥 |
| latch | 内存结构（页、索引、锁系统、日志） | 否 | `futex`、`rw_lock`、spin |

三者不可混谈：行锁等待不会消耗 CPU（睡眠），latch 竞争会消耗 CPU（自旋/上下文切换）。

## 2. 事务锁

- **记录锁**：唯一索引等值命中一行。
- **间隙锁**：RR 下范围条件锁住区间，防幻读。
- **next-key**：记录 + 前面的间隙；RR 默认。
- **插入意向锁**：并发插入同一间隙时冲突。
- **表意向锁 IS/IX**：DML 自动加，和表锁/DDL 互斥。
- **AUTO-INC**：自增分配，`innodb_autoinc_lock_mode` 影响并发插入。

诊断：

```
-- 不取 trx_query：SQL 原文含业务数据，不进证据包
SELECT TRX_ID,TRX_STATE,TRX_STARTED,TRX_WAIT_STARTED,TRX_MYSQL_THREAD_ID,TRX_WEIGHT
  FROM information_schema.INNODB_TRX\G
SELECT * FROM information_schema.INNODB_LOCK_WAITS\G
SHOW ENGINE INNODB STATUS\G   -- TRANSACTIONS / LATEST DETECTED DEADLOCK
```

要点：

- 锁等待高但 CPU 不高 → 业务冲突/长事务，不是“算力不足”。
- 死锁是正常保护机制；重点看访问顺序与索引，而不是简单重试。
- 长事务持有 read view 会阻塞 purge，间接造成 undo 膨胀与读放大。

## 3. MDL

- DML 持有 MDL 共享锁；DDL 需要排他，二者互斥。
- 长查询/未提交事务会让 DDL 排队，后续 DML 也可能被阻塞（队列效应）。
- 诊断：`SHOW PROCESSLIST` 的 `Waiting for table metadata lock`、
`performance_schema.metadata_locks`（若启用）。
## 4. InnoDB latch 与 10.11 实现

### 4.1 结构

| 类型 | 头文件 | 语义 |
| --- | --- | --- |
| `rw_lock` | `storage/innobase/include/rw_lock.h` | 读写 latch，`std::atomic<uint32_t> lock` |
| `srw_lock` | `storage/innobase/include/srw_lock.h` | 共享/排他，Linux futex 或平台原语 |
| `sux_lock` | `storage/innobase/include/sux_lock.h` | S/U/X 三态，支持递归 U/X |
| mutex | `sync/`/pthread | 短临界区 |

### 4.2 版本演进（重要）

| 版本 | 实现 | 影响 |
| --- | --- | --- |
| 10.3/10.4 | `rw_lock_t` + `lock_word`/`s_lock_word`（`sync0rw.h`） | 案例中的“顺序锁/拆分 lock_word”针对此 |
| 10.6+ / 10.11 | `rw_lock` 类，`std::atomic<uint32_t>`；`srw_lock`/`sux_lock` 模板 | 老字段不存在，补丁需重写 |

因此：

- 10.3 火焰图里的 `rw_lock_s_lock_func`/`lock_word` 热点名，在 10.11 可能变成
`rw_lock::read_lock`/`std::atomic` 相关符号。
- 移植优化前先在新版本采样，确认竞争是否仍集中在同一结构。

### 4.3 常见全局热点

| 结构 | 保护对象 | 典型触发 |
| --- | --- | --- |
| `buf_pool`（页 hash/LRU） | 页查找、LRU、free list | 高并发读、缓存不足 |
| `lock_sys` | 行锁 hash、等待队列 | 高冲突更新 |
| `trx_sys` | 事务/read view/历史列表 | 高并发事务、长事务 |
| `log_sys` | redo 缓冲与写入顺序 | 写密集 |
| `fil_system` | 表空间/文件 | DDL、打开表 |
| `dict_sys` | 数据字典 | DDL、首次访问表 |
| AHI 分区 | 自适应哈希 | 等值查询密集 |

## 5. 线程角色

用 `ps -L -p <pid> -o tid,comm,psr,pcpu` 或 `performance_schema.threads` 识别：

| 角色 | 名称线索 | 关注 |
| --- | --- | --- |
| 连接线程 | `mariadbd`（每连接） | 用户态计算、锁等待 |
| 线程池 worker | `threadpool` 相关 | 排队、stall |
| page cleaner | `page_cleaner` | 刷脏、checkpoint |
| purge | `purge` | undo 清理、历史列表 |
| log writer/flusher | `log_writer`/`log_flusher` | redo 写/刷 |
| IO 线程 | `ib_io`/`io_read`/`io_write` | 异步 IO 完成 |
| binlog dump | `Binlog dump` | 复制发送 |
| slave IO/SQL | `Slave_IO`/`Slave_SQL` | 复制回放 |
| wsrep | `wsrep` | Galera |

注意：线程名不是稳定 API；以版本实际输出为准，结合 CPU/等待/栈判断。

## 6. 从火焰图到机制

| 火焰图特征 | 可能机制 | 下一步证据 |
| --- | --- | --- |
| `futex`/`pthread_cond_wait` 高 | 锁/同步等待 | off-CPU 栈、`INNODB_LOCK_WAITS`、`SEMAPHORES` |
| `cas`/`ldadd`/`__aarch64_*` 高 | 原子竞争/引用计数 | 绑核前后对比、LSE 构建对比 |
| `memcpy`/`malloc`/`free` 高 | 数据拷贝/分配 | 结果集大小、临时表、分配器 |
| `btr_cur_search` 高 | B+tree 查找/回表 | 执行计划、逻辑读、覆盖索引 |
| `log_*`/`fsync` 高 | redo/日志刷盘 | `Innodb_log_waits`、IO await |
| `buf_*`/`LRU` 高 | 页缓存竞争/不足 | 物理读、pool 命中率 |
| `row_*`/`lock_*` 高 | 行锁/记录处理 | 锁等待、扫描行数 |
| `filesort`/`tmp` 高 | 排序/临时表 | `Created_tmp_disk_tables`、`Sort_merge_passes` |

## 7. 归因检查表

- 瓶颈在连接线程、后台线程还是线程池 worker？
- 是等锁（睡眠）还是自旋（CPU）？
- 行锁、MDL、latch 哪一类？对应哪个结构？
- 是否跨 NUMA / 跨 Die？
- 是否与 IO/日志刷盘同步（P99 抖动）？
- 换并发/换索引/换绑核后热点是否迁移？
- 结论是否在 10.11 重新验证过（而非照搬 10.3）？
