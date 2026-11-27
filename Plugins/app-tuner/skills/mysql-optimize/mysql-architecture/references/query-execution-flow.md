# 查询执行与读写链路

## 1. 连接与命令循环

```
accept() → THD 创建 → 认证 → handle_connection()
  → dispatch_command(COM_QUERY / COM_STMT_EXECUTE)
  → 解析 → 优化 → 执行 → 结果集写回
```

- `max_connections`、`back_log`、`thread_cache_size` 影响连接建立与线程复用。
- `--db-ps-mode=auto` 的 sysbench 走 prepared statement：`COM_STMT_PREPARE` 一次，
`COM_STMT_EXECUTE` 多次；受 `max_prepared_stmt_count` 限制。
- 连接线程在语句执行期间占用一个 OS 线程；没有 thread pool 时，高并发=高线程数。

## 2. SELECT 链路（point / range / join）

```
sql_parse → Sql_cmd_select::execute
  → JOIN::optimize（选择索引、join order）
  → JOIN::exec → 迭代器（TableScanIterator / IndexScanIterator；ref 访问走 RefIterator / EQRefIterator）
  → handler::ha_index_read_map / ha_rnd_next
  → ha_innobase::index_read / general_fetch
  → row_search_mvcc
      → AHI 命中（innodb_adaptive_hash_index=ON 且热键）→ 直接定位记录，跳过根→叶逐层查找
      → 未命中 → btr_pcur_open_with_no_init → btr_cur_search_to_nth_level
          → page_cur_search_with_match（页目录二分 + 记录比较）
      → 可见性判断（Read View / 锁）
      → 取聚簇索引行或回表
      → row_sel_store_mysql_rec（解码到 MySQL 行格式）
  → 表达式计算 / 排序 / 聚合
  → net_send / 结果集
```

关键点：

- **二级索引查询**先定位二级索引记录，再用主键回表；回表次数与结果集大小、索引覆盖
与否直接相关。
- **point select**：AHI 命中时由自适应哈希索引直接定位记录，跳过根到叶逐层查找；未命中才
经历页内二分、记录比较、Read View、行解码的完整 B+Tree 路径。高 QPS 时这些“小函数”
会累积成主要 CPU；归因前先用 AHI 命中状态区分分支，不要把完整路径算到所有点查上。
- **range/order/distinct** 可能触发 filesort：先算 sort key（`my_strnxfrm_*`），再排序；
`DISTINCT`/`GROUP BY` 还可能走 `my_hash_sort_*`。
- **join** 的驱动表、被驱动表索引、join buffer 由优化器决定；`EXPLAIN`/`EXPLAIN ANALYZE`
是核对执行计划的第一证据。

## 3. 写入链路（INSERT/UPDATE/DELETE → COMMIT）

```
ha_innobase::write_row/update_row/delete_row
  → 定位聚簇索引记录（B+Tree）
  → 生成 undo（旧版本） + 修改聚簇索引
  → 维护二级索引（可能进 change buffer）
  → 写 redo（mtr commit → log buffer）
COMMIT:
  → 两阶段：InnoDB prepare（redo 持久化策略）→ 写 binlog → InnoDB commit
  → binlog group commit：FLUSH → SYNC → COMMIT ORDER 三阶段排队
  → 按 sync_binlog / innodb_flush_log_at_trx_commit 决定 fsync
  → 释放锁、推进 purge
```

关键点：

- `innodb_flush_log_at_trx_commit=1` + `sync_binlog=1` 是“每个提交组都 fsync”的 durable
配置（同组事务共享一次 binlog fsync）；压测写场景的尾延迟常由这两处 fsync 与组提交效率决定。
`sync_binlog=N` 按 N 个提交组同步一次，不是 N 个事务；按事务数推算会高估 fsync 次数，
也会低估未同步事务数。
- 组提交把多个事务的 fsync 合并；队列/锁竞争高时，`m_lock_done` 等全局锁会成为瓶颈
（见源码优化 binlog ordered）。
- 大事务增加 undo、锁持有时间、purge 压力与 redo 体积；binlog 体积与 `binlog_row_image`
相关。
- Doublewrite 在页写失败时保证可恢复；NVMe 原子写可替代，但必须满足硬件/文件系统/页
大小条件。

## 4. 排序与 collation 路径

```
ORDER BY / GROUP BY / DISTINCT
  → 需要排序键：按 collation 分派 my_strnxfrm_*（把字符转成可比较权重；
     0900 系走 my_strnxfrm_uca_900，general/bin 系走 my_strnxfrm_unicode，
     多字节通用路径走 my_strnxfrm_mb）
  → 内存不足：写入临时文件（filesort merge）
  → DISTINCT/GROUP BY 哈希：my_hash_sort_*（生成哈希）
```

- 8.0.25/8.0.28 没有 `my_strnxfrm_utf8`/`my_strnxfrm_utf8mb4` 符号；`my_strnxfrm_*`
  只是 family 表述，实际按 collation 分派到上述三个函数。
- utf8/utf8mb4 的通用路径逐字符解码；ASCII 为主的字符串仍走多字节函数。
- aarch64 上可用 NEON 一次处理 16 字节 ASCII（见 space-skipping / NEON collation 补丁）。
- `innodb_sort_buffer_size`、`sort_buffer_size`、`max_length_for_sort_data`、
`tmp_table_size` 影响是否落盘与内存压力。
- 优化方向优先级：SQL/索引/查询语义 > collation 实现 > 微指令优化。

## 5. 复制与日志链路（影响写性能）

```
COMMIT → binlog write → group commit flush/sync
  → dump thread 读取 binlog → 发送 slave
  → slave IO thread 写 relay log → SQL/applier 应用
```

- `binlog_order_commits=ON` 保证提交顺序；组提交 leader/follower 通过条件变量同步。
- `max_binlog_size` 触发 rotate；预分配 binlog 可减少扩展文件的元数据开销（见补丁）。
- 半同步复制会增加提交等待；压测环境通常关闭。

## 6. 线程/锁边界速查

| 边界 | 典型等待/热点 | 证据 |
| --- | --- | --- |
| 连接/网络 | `net_send`、socket 写、客户端慢 | `perf`、`ss`、客户端 CPU |
| SQL 解析/优化 | `lex`、`JOIN::optimize`、统计信息 | `perf`、`EXPLAIN` |
| 行读取 | `row_search_mvcc`、`btr_cur_search`、`rec_get_offsets` | `perf`、`Innodb_rows_read` |
| Buffer Pool | 页 latch、LRU mutex、刷脏 | `perf`、`INNODB STATUS` |
| 事务/锁 | `lock_wait`、`trx_sys->mutex`、死锁 | `INNODB STATUS`、`performance_schema.data_lock_waits` |
| redo | `log_writer/log_flusher`、fsync | `perf`、`Innodb_os_log_*` |
| binlog/组提交 | `Commit_stage_manager`、`m_lock_done` | `perf`、`SHOW MASTER STATUS` |
| 排序 | `my_strnxfrm_*`、`my_hash_sort_*`、临时文件 | `perf`、`Sort_*` 状态 |

## 7. 用 EXPLAIN 核对（只读）

```
EXPLAIN FORMAT=TREE SELECT ...;
EXPLAIN ANALYZE SELECT ...;   -- 8.0.18+
SHOW STATUS LIKE 'Last_query_cost';
```

注意：`EXPLAIN ANALYZE` 会真实执行查询；生产环境先加 `LIMIT` 或在只读副本执行。
