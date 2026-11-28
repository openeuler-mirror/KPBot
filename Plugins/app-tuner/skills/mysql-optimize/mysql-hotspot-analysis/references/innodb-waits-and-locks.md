# InnoDB 等待、锁与日志热点

## 1. 自旋与 latch

| 符号/结构 | 含义 | 证据 |
| --- | --- | --- |
| `ut_delay` | InnoDB 自旋等待 | perf self 高；`innodb_spin_wait_delay`/`sync_spin_loops` |
| `pthread_mutex_*` | OS mutex 争用 | perf self/children；`perf lock`（可选） |
| `buf_pool` mutex | 页哈希/LRU 争用 | 高并发读/刷脏 |
| `trx_sys->mutex` | 事务系统/Read View | 高并发短事务 |
| `log_sys` mutex | redo 写/刷 | 写场景 |
| `dict_sys` mutex | 数据字典/表缓存 | DDL/打开表多 |
| `lock_sys` latch | 行锁哈希 | 热点行/大量锁 |

判读：

- `ut_delay` 高不一定是“锁不够”，可能是持锁者被调度/IO 阻塞；需结合 off-CPU。
- 自旋参数（`innodb_spin_wait_delay`/`innodb_sync_spin_loops`）是双刃剑：
CPU 有富余时可增大，CPU 饱和时增大可能更差。
- 页 latch 竞争可通过减少热点页访问（分区/拆分/缓存）或增加实例缓解。

## 2. 行锁与死锁

```
SELECT * FROM performance_schema.data_lock_waits;
SELECT * FROM performance_schema.data_locks LIMIT 50;
SELECT * FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
SHOW ENGINE INNODB STATUS\G   -- TRANSACTIONS / LOCK WAIT
```

常见根因：

- 无索引/索引选择差 → 扫描并锁大量记录。
- 热点行更新 → 串行排队，CPU 不高但 TPS 低。
- 长事务 → 锁持有久、purge 落后。
- 死锁检测（`innodb_deadlock_detect=ON`）在高并发下开销显著。
- RR 间隙锁在范围更新/插入时增加冲突。

处置方向（由 optimization 实施）：

- 优化 SQL/索引，缩小锁定范围。
- 拆事务、降隔离级别（业务允许时）。
- 热点行拆分/排队/异步化。
- 评估死锁检测开关（风险：死锁等待变长）。

## 3. Buffer Pool 与刷脏

| 证据 | 含义 |
| --- | --- |
| `Innodb_buffer_pool_reads/read_requests` | 未命中率 |
| `Innodb_buffer_pool_pages_free/dirty` | 空闲/脏页 |
| `Innodb_buffer_pool_wait_free` | 无空闲页等待 |
| `Innodb_log_waits` | log buffer 空间不足等待（`innodb_log_buffer_size`），不是 redo 文件空间不足 |
| `Innodb_data_pending_fsyncs` | 未完成 fsync |
| `iostat %util/await` | 盘压力 |

刷脏热点：

- `page_cleaner` 在脏页高水位/checkpoint 时活跃；刷脏不足 → 用户线程被迫刷（`buf_flush`）。
- `innodb_io_capacity` 过低 → 刷脏慢、脏页积压；过高 → IO 竞争。
- `innodb_max_dirty_pages_pct(_lwm)` 决定预刷水位；写多可调高但恢复/尾延迟风险。
- `innodb_lru_scan_depth` 与 `page_cleaners` 影响扫描/并行度。

## 4. redo 与提交

| 符号 | 含义 |
| --- | --- |
| `log_write_up_to` | 等待 redo 写到指定 LSN |
| `log_writer` | 把 log buffer 写文件 |
| `log_flusher` | fsync |
| `os_file_fsync_posix`/`fsync` | 系统调用 |
| `Innodb_os_log_written` | redo 写入量 |
| `Innodb_log_waits` | log buffer 空间不足等待（`innodb_log_buffer_size`），不是 redo 文件空间不足 |

`innodb_flush_log_at_trx_commit`：

- `1`：每提交 fsync；最安全，写延迟最高。
- `2`：每提交写 OS cache，每秒 fsync；崩溃可能丢 1 秒。
- `0`：每秒写+fsync；崩溃可能丢 1 秒。

热点判读：

- `log_flusher` 的 `fsync` 占比高 → 盘延迟/同步策略。
- `log_writer` 高但 flusher 低 → 可能是 log buffer/写入量。
- 组提交效率低 → 队列/锁/同步等待（见 §5）。
- `Innodb_log_waits` 递增 → 优先核对 `innodb_log_buffer_size` 与写入速率；
  redo 文件容量不足需另看 checkpoint/刷脏积压证据，不要据此直接加大 redo 文件。

## 5. binlog 组提交

| 符号/结构 | 含义 |
| --- | --- |
| `Commit_stage_manager::enroll_for` | 事务进入阶段队列 |
| `m_lock_done` / 条件变量 | follower 等待 leader |
| `mysql_cond_wait/broadcast` | 组提交唤醒 |
| `MYSQL_BIN_LOG::write_event` | binlog 写入 |
| `fsync`（binlog） | `sync_binlog` |

判读：

- 高并发写时，`m_lock_done`/条件变量可能成为串行点；上下文切换高。
- `sync_binlog=1` 时 fsync 是尾延迟主因；组提交可合并多个事务。
- binlog rotate/文件扩展可能引入额外元数据开销（见 preallocate 补丁）。
- 任何组提交改动都必须验证提交顺序、崩溃恢复、主从一致性。

## 6. undo / purge

| 证据 | 含义 |
| --- | --- |
| `History list length` | 未 purge 的旧版本量 |
| `Trx id counter` | 事务增长 |
| `purge_*` 线程 CPU | purge 压力 |
| 长事务 | 阻止 purge 推进 |

长事务/大查询持有旧 Read View → undo 膨胀 → 读路径回溯变长、磁盘涨。
处置：拆事务、监控、必要时 kill（确认业务影响）。

## 7. 排序/临时表

| 证据 | 含义 |
| --- | --- |
| `Sort_merge_passes` | 归并次数（落盘） |
| `Created_tmp_disk_tables` | 磁盘临时表 |
| `Select_full_join`/`Select_scan` | 无索引/全表 |
| `my_strnxfrm_*`/`my_hash_sort_*` | sort key/哈希 |

优化方向：索引/ SQL 改写 > 增大 sort/tmp 缓冲 > collation 实现优化。
注意：增大缓冲可能只是把落盘推迟，需看实际行数与内存。

## 8. 归因检查表

- 场景/数据集/线程/绑核已记录
- on-CPU 与 off-CPU 都有证据
- 业务线程 vs 后台线程占比明确
- 锁等待/长事务已排除或确认
- redo/binlog/刷脏路径已核对
- Buffer Pool 命中率与磁盘读已核对
- 排序/临时表落盘已核对
- 客户端/IRQ 瓶颈已排除
- 结论有对照实验或第二个证据源
