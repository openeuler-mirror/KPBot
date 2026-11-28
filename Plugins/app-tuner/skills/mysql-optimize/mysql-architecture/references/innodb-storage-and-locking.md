# InnoDB 存储、事务、锁与日志

## 1. 页与行格式

- 页大小由 `innodb_page_size` 决定（默认 16K；可选 4K/8K/32K/64K）：**仅初始化参数**，
只能在新建实例初始化时确定。已有实例的页大小不可修改——改值后重启会因与现有表空间页大小
不匹配而无法启动；需要变更时新建实例并迁移数据，它不是“重启生效项”。
- 聚簇索引叶子页存放完整行；二级索引叶子页存放索引列 + 主键。
- 行格式 `DYNAMIC`（8.0 默认）：长变长列可溢出到 off-page。
- 记录头含 `info_bits`、`n_owned`、`heap_no`、`next_record`；`REC_INFO_MIN_REC_FLAG`
标记最小记录，比较时需特殊处理。

## 2. B+Tree 查找

> 分支前提：以下是 AHI 未命中时的完整路径。`innodb_adaptive_hash_index=ON` 且查找键命中
> 自适应哈希时，InnoDB 直接定位记录，跳过根→叶逐层查找；AHI 关闭、冷键或条件不满足时才
> 回退到该路径。热点归因先区分 AHI 命中与回退两类，不要把完整 B+Tree 路径写成点查必经路径。

```
btr_cur_search_to_nth_level
  → 从根页向下，每层 page_cur_search_with_match
      → 页目录二分定位 slot
      → 记录链线性微调
      → cmp_dtuple_rec / tuple->compare
  → 到达叶子页，返回 cursor（page_cur_t）
```

可优化点：

- `cmp_dtuple_rec` 逐字段比较，涉及 collation、NULL 位、变长长度；固定长度且二进制
可比较的字段可直接 memcmp（fast-tuple 补丁）。
- `rec_get_offsets` 每次重新计算字段偏移；传入已知 compact 标志可省去重复判断
（rec_get_offsets 补丁）。
- 页内比较依赖 `matched_fields` 复用；实现错误会导致“少做工作”的假性能，必须用
debug assert 与 MTR 校验。

## 3. Buffer Pool 与刷脏

- 结构：`buf_pool_t`（每 instance 一个）+ 页哈希 + LRU（young/old）+ flush list。
- 读：页哈希命中 → 加页 latch（S/X）→ 返回；未命中 → 从磁盘读 → 插入 LRU。
- 写：修改页 → 标脏 → 加 flush list → redo 先落盘（WAL）→ 后台刷脏。
- 刷脏触发：脏页比例（`innodb_max_dirty_pages_pct` / `_lwm`）、redo 空间、
LRU 尾部、checkpoint、`innodb_io_capacity`。
- 关键参数：`innodb_buffer_pool_size`、`instances`、`chunk_size`、
`innodb_lru_scan_depth`、`innodb_page_cleaners`、`innodb_io_capacity(_max)`、
`innodb_flush_neighbors`、`innodb_flush_method`。

经验：

- Buffer Pool 通常为内存的 50%–70%，需给连接/排序/临时表/OS 留余量。
- 实例数不是越多越好：小 buffer pool 配过多 instance 会增加管理开销。
- `O_DIRECT` 避免 double cache，但小 IO 可能变慢；需按负载验证。

## 4. MVCC 与 Read View

- 每行含 `DB_TRX_ID`、`DB_ROLL_PTR`；旧版本通过 undo 链回溯。
- Read View 记录 `m_ids`（活跃事务）、`m_up_limit_id`、`m_low_limit_id`、
`m_creator_trx_id`；可见性判断据此决定读当前行还是 undo 版本。
- RC：每条语句新建/复用视图；RR：事务首次读建立并复用。
- `MVCC::view_open` 在 `trx_sys->mutex` 下维护 `m_views` 列表；高并发短查询会频繁
open/close 视图，形成 `trx_sys->mutex` 竞争。
- 优化思路：用原子版本号判断“建视图的数据是否变化”，不变则复用，避免取全局 mutex
（见源码优化 Read View version）。前提是语义等价，必须用 MTR 与高并发只读 A/B 验证。

## 5. 锁与等待

| 锁类型 | 触发 | 观测 |
| --- | --- | --- |
| 行锁（record lock） | UPDATE/DELETE/SELECT FOR UPDATE | `data_locks`、`INNODB STATUS` |
| 间隙锁（gap） | RR 下范围条件 | `data_locks` 的 GAP |
| 插入意向锁 | INSERT 并发同间隙 | 死锁日志 |
| 表锁/元数据锁（MDL） | DDL、长事务 | `metadata_locks` |
| 自旋锁（latch） | 页/哈希/日志结构 | `perf` 的 `ut_delay`、`pthread_mutex` |

常见根因：

- 无索引/索引选择差 → 扫描大量记录并加锁。
- 长事务持有锁 → 阻塞 DDL 与 purge。
- 热点行更新 → 行锁排队，CPU 不一定高。
- 死锁检测开销（`innodb_deadlock_detect`）在高并发下可能显著。

## 6. redo / undo / doublewrite

| 结构 | 写入时机 | 作用 | 关键参数 |
| --- | --- | --- | --- |
| redo | 页修改的 mtr commit | 崩溃恢复、WAL | `innodb_log_file_size`、`innodb_log_buffer_size`、`innodb_flush_log_at_trx_commit` |
| undo | 修改前记录旧值 | 回滚、MVCC | `innodb_undo_tablespaces`、purge 线程 |
| doublewrite | 脏页刷盘前 | 页撕裂保护 | `innodb_doublewrite` |
| binlog | 提交阶段 | 复制、PITR | `sync_binlog`、`binlog_format` |

`innodb_flush_log_at_trx_commit`：

- `1`：每次提交 fsync redo，最安全，写延迟最高。
- `2`：每次提交写 OS cache，每秒 fsync；崩溃可能丢 1 秒。
- `0`：每秒写+fsync；崩溃可能丢 1 秒且丢事务。

`sync_binlog`：`1` 每个提交组 fsync 一次 binlog；`N` 每 N 个提交组（binlog flush 组）fsync
一次，不是每 N 个事务——同组事务共享这一次 fsync，按事务数推算会高估 fsync 次数、
低估未同步事务数；`0` 交给 OS。生产 durable 通常两者都为 1，压测可按目标调整并明确数据风险。

## 7. binlog 组提交

```
事务提交 → enroll_for(FLUSH_STAGE) → leader 收集队列
  → FLUSH：写 binlog 文件（可能 group）
  → SYNC：按 sync_binlog 决定 fsync
  → COMMIT ORDER：按顺序让 follower 完成 InnoDB commit
```

- 队列与条件变量由 `Commit_stage_manager` 管理；follower 等待 leader 信号。
- 高并发写时，全局 `m_lock_done` 与单一条件变量会造成排队与上下文切换。
- 优化思路：按队列分配多组 pending lock/cond，减少全局锁竞争（见 binlog ordered 补丁）；
或用 binlog 预分配减少文件扩展开销（见 preallocate 补丁）。
- 任何组提交改动都必须验证：提交顺序、崩溃恢复、主从一致性、MTR 复制用例。

## 8. Purge 与长事务

- purge 线程按 `low_limit_no` 清理不再需要的 undo。
- 长事务/大查询持有旧 Read View → purge 无法推进 → undo 膨胀、历史版本链变长 →
读变慢、磁盘涨。
- 观测：`SHOW ENGINE INNODB STATUS` 的 `History list length`、`Trx id counter`；
`information_schema.innodb_trx` 的长事务。
- 处置：拆事务、加监控、必要时 kill 长事务（需确认业务影响）。

## 9. 只读核对清单

```
SHOW ENGINE INNODB STATUS\G
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_pages%';
SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%';
SHOW GLOBAL STATUS LIKE 'Innodb_log%';
SHOW GLOBAL STATUS LIKE 'Innodb_row_lock%';
SELECT * FROM performance_schema.data_lock_waits;
SELECT * FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
```
