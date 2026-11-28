# 源码优化总览（已交付 patch）

>
> 所有 patch 均以 `mysql-8.0.25` 为基线，且多数带 `__aarch64__ && __linux__` 条件编译；
> 合并前必须确认目标版本、上下文与语义。收益需在目标环境 MTR + 同口径 A/B 复测。

## 1. 补丁清单

| Patch | 位置 | 机制 | 适用场景 | 风险 |
| --- | --- | --- | --- | --- |
| `0001-fast-tuple` | `btr0cur.cc`、`page0cur.cc`、`rem0cmp.ic`、`row0sel.cc` | 固定长度、内存可比较字段直接 memcmp，跳过逐字段 collation | Point/range 读 | 比较语义必须完全等价；仅特定字段/非 min-rec |
| `0001-innodb-read-view-version` | `ha_innodb.cc`、`read0read.cc`、`trx0sys/trx` | 原子 `readview_version` 复用 Read View，避免 `trx_sys->mutex` | 高并发只读/RC | 视图复用语义；需高并发与 MTR 验证 |
| `0002-space-skipping` | `row0mysql.cc`、`ctype-mb.cc`、`ctype-utf8.cc` | NEON ASCII 批量跳过/计数/生成 sort key/哈希/比较 | RO 的 order/distinct | 仅 ASCII 快路径；非 ASCII 回退；collation 语义 |
| `0004-binlog-ordered` | `rpl_commit_stage_manager.*`、`transaction_info.*` | 每个队列 32 组 pending lock/cond，替代全局 `m_lock_done` | RW/WO 高并发 | 组提交顺序/唤醒/崩溃恢复 |
| `0005-rec_get_offsets` | `rec.cc/h`、`page0cur.cc`、`row0sel.cc` | 传入 compact 标志，省去重复 `dict_table_is_comp` | 读路径 | 仅在 compact 判断正确时使用 |
| `0007-binlog-preallocate` | `binlog*`、`basic_*stream`、`sys_vars` | `fallocate` 预分配 binlog，读取有效 EOF | 写多/大 binlog | 磁盘占用；崩溃/复制读取边界 |

## 2. fast-tuple：直接内存比较

链路：

```
row_search_mvcc / btr_cur_search_to_nth_level
  → dtuple_fetch_partial_cmp_rec（取参与比较的固定字段）
  → page_cur_search_with_match（rec_direct_memcmp）
  → row_search_mvcc（ut_memcmp）
```

条件（缺一不可）：

- `index->cached`、compact 表、无 instant cols、非 min-rec。
- 字段固定长度、NOT NULL、类型内存可比较（INT/BINARY/FIXBINARY/SYS 等）、
非 multi-value、升序。
- 变长/字符/collation 字段必须回退原比较。
- debug 下用 `cmp_dtuple_rec` 做 `ut_ad` 等价校验。

收益：Point/range 减少每记录逐字段比较与 offset 计算；对字符/变长索引无效。
风险：比较语义偏差会“少做工作”导致假性能或错误结果，必须 MTR + 结果对账。

## 3. Read View version：减少事务系统锁

原逻辑：`MVCC::view_open` 在 `trx_sys->mutex` 下判断是否复用视图（要求无活跃 RW 事务），
高并发短查询频繁 open/close。

补丁：

- `trx_sys->readview_version` 原子计数，在 RW 事务加入/移除时递增。
- `ReadView` 记录创建时版本；`view_open` 先无锁比较版本，相同则复用；
不同再取 mutex 重建。
- `view_close` 不再包 `trx_sys->mutex`（`view_close(view, false)`）。
- `clone_oldest_view` 遍历未关闭视图，避免不必要重建。

收益：高并发只读/RC 降低 `trx_sys->mutex` 与上下文切换。
风险：可见性语义必须等价；需 MTR 事务/隔离用例 + 高并发只读 A/B + 长事务/purge 场景。
## 4. space-skipping + NEON collation

内容：

- `row_mysql_store_col_in_innobase_format` 用 `skip_trailing_space` 替代逐字节去尾空格。
- `ctype-mb.cc`：`my_numchars_mb`/`my_charpos_mb` ASCII 16 字节批量计数。
- `ctype-utf8.cc`：`my_strnxfrm_unicode` ASCII 8 字符批量生成权重；
`my_hash_sort_utf8/utf8mb4` 16 字节批量哈希；`my_strnncollsp_utf8/utf8mb4`
用 `skip_space` 批量跳过空格。

收益：RO 的 ORDER BY/DISTINCT/GROUP BY 在 ASCII 为主时降低 sort key/哈希成本。
风险：非 ASCII 必须回退；collation 语义（大小写、重音、尾空格）必须 MTR 对账；
只优化实现不改 SQL，收益受数据分布限制。

## 5. binlog ordered：降低组提交全局锁争用

原逻辑：所有 follower 在 `m_lock_done` 上等待，`signal_done` 持全局锁唤醒。

补丁：

- 每个 `Mutex_queue` 初始化 32 组 `m_pending_locks/m_pending_conds`。
- 事务入队时按批次分配 pending lock/cond；follower 在自己那一组等待。
- `signal_done` 按事务分组加锁/广播，减少全局锁与惊群。
- `m_lock_done`/`m_stage_cond_binlog` 在 aarch64 Linux 下不再使用（debug 保留）。

收益：高并发写降低组提交排队/上下文切换。
风险：提交顺序、唤醒丢失、崩溃恢复、主从一致性；需 MTR 复制用例 + 写场景 A/B +
高并发压力 + 故障注入（kill/重启）。

## 6. rec_get_offsets：省去重复 compact 判断

- `rec_get_offsets_func` 每次调用判断 record 状态/字段数；`rec_get_offsets_with_comp`
接收已知 `compact` 标志，直接走对应分支。
- 调用点：`row_search_mvcc`、`page_copy_rec_list_end_to_created_page` 等热路径。

收益：读路径减少分支与重复判断；收益通常较小，适合与其他读优化叠加。
风险：compact 标志错误会导致 offset 解析错误；必须在 compact/非 compact 表上 MTR 覆盖。

## 7. binlog preallocate：减少文件扩展开销

- `IO_CACHE_ostream::preallocate_file` 用 `fallocate`（`FALLOC_FL_WRITE_ZEROS` 或
`ZERO_RANGE`）预分配 `max_binlog_size`。
- 读取侧 `set_eof`/`get_valid_log_file_size` 把“有效末尾”与物理 EOF 区分，
避免把预分配零区当事件。
- `reclaim_preallocated_file` 在关闭时 `ftruncate` 到实际写入位置。
- 新变量 `enable_binlog_preallocate`（补丁中默认 true，只读全局）。

收益：写多/大 binlog 减少扩展/元数据/碎片开销。
风险：磁盘预留占用（关闭时回收）；崩溃/复制读取边界必须正确；需 binlog 解析、
`mysqlbinlog`、主从、崩溃恢复测试。

## 8. 挖掘新优化点的思路

1. perf 热点函数 → 源码 → 判断是否 aarch64 特化/通用结构问题。
2. 优先“去分配/去哈希/去重复计算/减少锁”，再做指令级。
3. 检查语义边界（collation、NULL、变长、min-rec、隔离级别、复制顺序）。
4. 加 `ut_ad`/debug 对照，先证明等价，再谈性能。
5. 小步提交：一个 patch 一个机制，单独 A/B，保留负结果。
