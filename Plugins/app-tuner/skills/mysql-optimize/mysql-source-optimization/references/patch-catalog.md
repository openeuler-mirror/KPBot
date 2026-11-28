# 补丁目录与实施细节

>
> 基线：MySQL 8.0.25/8.0.28。本 skill **不附带补丁原文**：下列条目是同类改动的
> **机制示例/实施模板**，不是可直接应用的交付补丁，也不代表已验证的收益。实际操作前必须由
> 操作者提供与目标源码树版本匹配的真实 `.patch` 文件，经 `scripts/apply_patch.sh --patch` 传入；
> 脚本会先校验文件存在，再用 `git apply --index --check` 同时检查工作树与索引。合并到其他版本时
> 先核对上下文，所有收益需在目标环境复测，不能直接引用他处百分比。

## 1. fast-tuple（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`storage/innobase/btr/btr0cur.cc`、`page/page0cur.cc`、`include/rem0cmp.h/.ic`、
`include/data0type.ic`、`include/page0cur.h`、`row/row0sel.cc`。

**机制**：

- `dtuple_fetch_partial_cmp_rec`：从 dtuple 取“参与比较的固定字段”，连续则原地引用，
否则拷贝到 255B 栈缓冲。
- `page_cur_search_with_match` 增加 `tuple_rec/tuple_rec_size`：页内二分时用
`rec_direct_memcmp` 直接比较内存，跳过 `rec_get_offsets` + 逐字段 collation。
- `row_search_mvcc` 命中记录后用 `ut_memcmp` 比较；min-rec 特殊处理。
- `dtype_is_memcmp_deterministric` 限定类型：INT/BINARY/FIXBINARY/SYS；GIS 非 MBR；
BLOB 需 binary。

**条件（不满足回退原逻辑）**：

- `index->cached`、compact 表、无 instant cols、`n_fields>0`、非 min-rec。
- 每个比较字段：固定长度、NOT NULL、内存可比较、非 multi-value、升序。
- debug 下用 `cmp_dtuple_rec` 做 `ut_ad` 等价校验。

**适用**：Point/range 的整数/二进制主键或索引；字符/变长/降序索引无效。

**风险**：类型判断漏项会导致比较结果错误（可能“少做工作”表现为假性能）。必须覆盖
NULL/变长/字符/降序/min-rec/instant/multi-value 的 MTR 与结果对账。

**验证**：MTR innodb 相关 + 自定义边界用例；Point/RO A/B；debug 构建跑
`ut_ad` 对照。

## 2. Read View version（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`handler/ha_innodb.cc`、`include/read0types.h`、`include/trx0sys.h`、
`read/read0read.cc`、`trx/trx0sys.cc`、`trx/trx0trx.cc`。

**机制**：

- `trx_sys->readview_version` 原子计数；RW 事务加入/移除时 `fetch_add`。
- `ReadView` 增加 `m_version`，`prepare` 时记录。
- `MVCC::view_open`：先无锁比较版本，相同则复用；不同再取 `trx_sys_mutex_enter` 重建。
- `view_close` 不再包 `trx_sys->mutex`（`view_close(view, false)`）。
- `clone_oldest_view` 遍历未关闭视图，避免不必要重建。

**适用**：高并发只读/RC 短查询；`trx_sys->mutex` 是热点时。

**风险**：视图复用条件必须与“源数据未变化”严格等价；长事务/purge/复制场景需验证。
RC/RR 语义、`m_low_limit_id`、closed 状态顺序都要 MTR 覆盖。

**验证**：MTR 事务/隔离/复制用例；高并发只读 A/B；长事务 + purge 场景；debug 断言。

## 3. space-skipping + NEON collation（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`row/row0mysql.cc`、`strings/ctype-mb.cc`、`strings/ctype-utf8.cc`。

**机制**：

- `skip_trailing_space` 替代逐字节去尾空格（含 `n_chars` 偏移版本）。
- `my_numchars_mb`/`my_charpos_mb`：UTF-8 非 NONASCII 时 16B NEON 判断全 ASCII，
批量计数/定位；非 ASCII 回退逐字符。
- `my_strnxfrm_unicode`：ASCII 8 字符批量生成 16B 排序权重（含大小写/`LOWER_SORT`）。
- `my_hash_sort_utf8/utf8mb4`：16B 批量哈希；非 ASCII 回退逐字符。
- `my_strnncollsp_utf8/utf8mb4`：`skip_space` 16B/8B/逐字节批量跳过尾空格。

**条件**：`__aarch64__ && __linux__`；`!(cs->state & MY_CS_NONASCII)` 才走批量；其余回退。

**适用**：RO 的 ORDER BY/DISTINCT/GROUP BY，ASCII 为主；`utf8`/`utf8mb4`。

**风险**：collation 语义（大小写、重音、尾空格、`MY_CS_LOWER_SORT`）必须与 MTR 一致；
非 ASCII/边界长度（<16/<8）走回退；批次跨边界不能越界。

**验证**：MTR charset/collation 用例；构造 ASCII/非 ASCII/混合/短串/长串；
排序与 DISTINCT 结果对账；RO A/B（含/不含 order、distinct）。

## 4. binlog ordered（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`sql/rpl_commit_stage_manager.cc/.h`、`sql/transaction_info.cc/.h`。

**机制**：

- `Mutex_queue` 增加 32 组 `m_pending_locks/m_pending_conds` 与 `m_to_assign_lock`。
- `append` 按批次给事务分配 pending lock/cond。
- follower 在各自 pending lock 上等待，不再全部抢 `m_lock_done`。
- `signal_done` 按事务分组加锁、收集 cond 后广播；`COMMIT_ORDER_FLUSH_STAGE` 仍用
`m_stage_cond_commit_order`。
- aarch64 Linux 下移除 `m_lock_done`/`m_stage_cond_binlog`（debug 保留）。

**适用**：RW/WO 高并发、组提交排队/上下文切换高。

**风险**：提交顺序、条件变量唤醒丢失、`tx_commit_pending` 状态、崩溃恢复、主从一致。
`m_pending_commit_lock` 分配/重置必须成对；队列边界不能越界。

**验证**：MTR 复制/组提交用例；高并发写 A/B；故障注入（kill/重启）；主从一致性；
`SHOW MASTER STATUS`（8.0.25/28 基线语法）/ `mysqlbinlog` 顺序检查。

## 5. rec_get_offsets（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`rem/rec.cc/.h`、`page/page0cur.cc`、`row/row0sel.cc`。

**机制**：

- 新增 `rec_get_offsets_with_comp(rec, compact, index, ...)`：已知 compact 时直接走
对应分支，省去 `dict_table_is_comp` 重复判断。
- 热路径调用点替换：`row_search_mvcc` 多处、`page_copy_rec_list_end_to_created_page`。

**条件**：`__aarch64__ && __linux__`；`compact` 必须来自正确来源（表/记录）。

**风险**：compact/非 compact 判断错误会导致 offset 解析错误；仅改性能路径，语义不变
但必须覆盖两种表格式。

**验证**：MTR 混合 compact/非 compact 表；读路径 A/B；debug 断言。

## 6. binlog preallocate（示例机制；仓内无补丁文件，补丁由操作者提供）

**位置**：`sql/basic_istream.*`、`basic_ostream.*`、`binlog*`、`binlog_istream.*`、
`binlog_ostream.h`、`binlog_reader.*`、`rpl_binlog_sender.*`、`mysqld.cc/.h`、
`sys_vars.cc`、`client/mysqlbinlog.cc`。

**机制**：

- 打开新 binlog 时 `preallocate_file(max_size)`，用 `fallocate`
（`FALLOC_FL_WRITE_ZEROS` 或 `ZERO_RANGE`）预分配。
- 读取侧 `set_eof`/`get_valid_log_file_size`：把“有效末尾”设为 `binlog_end_pos + encrypted_header`，避免把预分配零区当事件。
- `is_preallocated_log_eof`：读到零区且预分配时返回 EOF。
- 关闭时 `reclaim_preallocated_file` 用 `ftruncate` 回收未用空间。
- 新变量 `enable_binlog_preallocate`（补丁默认 true，只读全局）。

**适用**：写多/大 binlog、rotate 频繁、文件扩展开销明显。

**风险**：磁盘预留占用（关闭回收）；崩溃/复制读取边界；加密头偏移；`mysqlbinlog`
读取；主从 sender 的 EOF 判断。预分配失败需安全回退且不破坏文件。

**验证**：MTR binlog/复制用例；`mysqlbinlog` 解析；主从增量；崩溃恢复；
写场景 A/B；磁盘空间监控。

## 7. 新补丁模板（示例 diff，非交付补丁）

```
diff --git a/path/file.cc b/path/file.cc
--- a/path/file.cc
+++ b/path/file.cc
@@
+#if defined(__aarch64__) && defined(__linux__)
+  // 快路径
+#else
+  // 原逻辑（默认回退）
+#endif
```

要求：

- 默认回退；条件编译限定平台。
- 注释说明“为什么等价/边界”。
- debug 下加对照断言（可编译关闭）。
- 不改变持久化格式/协议/复制顺序，除非有完整验证。

## 8. 验证矩阵

| 补丁类型 | 必跑 |
| --- | --- |
| 比较/排序 | MTR charset/collation + 结果对账 + RO A/B |
| 视图/事务 | MTR 事务/隔离/复制 + 长事务/purge + 高并发只读 A/B |
| 组提交 | MTR 复制 + 高并发写 A/B + kill/重启 + 主从一致性 |
| binlog | MTR binlog/复制 + mysqlbinlog + 崩溃恢复 + 写 A/B |
| offset/读路径 | MTR 混合表格式 + 读 A/B + debug 断言 |
| 构建参数 | 版本/符号/md5 + 四场景 A/B + 负收益回滚 |
