# 场景热点与瓶颈迁移

>
> 百分比与构成来自特定鲲鹏/MySQL 8.0 实测，仅用于建立假设；不同数据/参数/版本会变化。

## 1. Point（主键等值，高 QPS）

| 层 | 典型热点 | 机制 |
| --- | --- | --- |
| SQL 层 | 解析/优化、prepare 复用、表达式 | 每查询固定开销 |
| handler | `ha_innobase::index_read` | Server→InnoDB 边界 |
| 读路径 | `row_search_mvcc`、`btr_cur_search_to_nth_level`、`page_cur_search_with_match`、`cmp_dtuple_rec`、`rec_get_offsets` | 页内二分/记录比较/offset |
| 事务 | Read View open/close、`trx_sys->mutex`、可见性 | 高并发短事务 |
| Buffer Pool | 页 latch、哈希查找 | 热点页竞争 |
| 网络/IRQ | `net_send`、`sendmsg`、软中断 | 小包高 PPS |
| AHI | 哈希查找/维护 | 高 CPU 读多时可能负收益 |

迁移：

- 并发↑：从计算转向 IRQ/网络/锁。
- AHI 开启且命中高：`btr_search` 相关上升；关闭后 B+Tree 比较上升。
- 绑核不当：跨 NUMA/IRQ 抢核导致 P95 抖动。

## 2. 只读（含 range/order/distinct）

| 层 | 典型热点 | 机制 |
| --- | --- | --- |
| 扫描 | `row_search_mvcc`、范围比较、回表 | 扫描行数×回表 |
| 排序 | `filesort`、`my_strnxfrm_any_uca`、`my_hash_sort_any_uca`、`my_strnncollsp` | sort key/哈希/比较 |
| 临时表 | `create_tmp_table`、`tmp_table_size` | 内存不足落盘 |
| 优化器 | `JOIN::optimize`、统计信息 | 计划选择 |
| Buffer Pool | 命中率、页替换 | 工作集 vs 内存 |
| 网络 | 结果集发送 | 大结果集 |

关键事实：

- `order_ranges`/`distinct_ranges` 语句占比小，但单语句处理行数可能很大；
排序键生成是逐字符/逐行成本。
- 对照方法（同口径 A/B）：只读 32 线程下对比“开/关 order/distinct range”的 TPS 与 P95，
  先确认字符排序路径是否成为热点；具体数值按当轮同口径测量记录（含机型/版本/负载与来源），
  此处不写死收益百分比。这是 query-mix/字符排序路径的诊断发现，**不是**服务器参数收益。
- utf8/utf8mb4 ASCII 快路径（NEON）可降低 `my_strnxfrm`/`my_hash_sort` 成本，
但语义必须由 MTR 校验。

## 3. 读写（RW）

| 层 | 典型热点 | 机制 |
| --- | --- | --- |
| 锁 | 行锁等待、`lock_sys`、死锁检测 | 热点行/无索引/长事务 |
| 事务 | `trx_sys`、undo、purge | 版本链/回滚段 |
| redo | `log_writer`、`log_flusher`、`log_write_up_to`、`fsync` | 提交持久化 |
| binlog | 组提交队列、`m_lock_done`、binlog 写/rotate | 三阶段同步 |
| 刷脏 | `page_cleaner`、`buf_flush_*`、LRU | 脏页水位/IO |
| doublewrite | `dblwr` 写 | 页原子性 |
| CPU | 解析/优化、行解码、索引维护 | 写路径计算 |

迁移：

- `sync_binlog=1`/`flush_log_at_trx_commit=1`：热点从 CPU 转向 fsync/组提交。
- 提高 `innodb_io_capacity`：刷脏更积极，可能降低脏页但增加 IO 竞争。
- 长事务：purge 落后、undo 膨胀、读变慢。

## 4. 只写（WO）

| 层 | 典型热点 | 机制 |
| --- | --- | --- |
| redo | 写 log buffer、fsync | 每次提交持久化 |
| binlog | 写文件、rotate、组提交 | 逻辑日志 |
| doublewrite | 两次写 | 页撕裂保护 |
| 刷脏 | page cleaner、checkpoint | 脏页比例/redo 空间 |
| 索引 | 二级索引维护、change buffer | 随机写 |
| IO | `io_submit`、`pread/pwrite` | 盘/队列 |

写场景 CPU 常不满；P99 由 fsync、组提交排队、doublewrite、盘延迟决定。

## 5. 线程占比速查

| 线程 | Point/RO | RW/WO |
| --- | --- | --- |
| `handle_connection` | 高 | 中高 |
| `log_writer`/`log_flusher` | 低 | 高（提交时） |
| `page_cleaner` | 低 | 中高（脏页） |
| `purge_*` | 低 | 中（长事务/写多） |
| `dict_stats` | 低 | 低 |
| IRQ/softirq | 高 QPS 时高 | 中 |

用 `ps -Lo` 的 `pcpu` 与 `perf -t` 加线程号分别确认，不要只看进程总 CPU。

## 6. x86 与 ARM 差异

- ARM（Kunpeng）上 LSE/CRC、NEON、页大小（64K）会影响原子/字符串/内存路径；
同一热点占比可能不同。
- x86 上 `rdtsc`、`crc32`、AVX 字符串路径与 ARM 不同；不要直接套用百分比。
- 虚拟化/容器下 PMU、cache 事件可能不可用；以调用栈与等待为主。
- 64K 页可能放大内部碎片/分配粒度，影响 Buffer Pool 与临时表行为。

## 7. 热点迁移实验设计

| 变量 | 观察 | 目的 |
| --- | --- | --- |
| 并发 32→64→128 | IRQ/锁/页 latch 是否上升 | 判断扩展瓶颈 |
| 加索引/改 SQL | 扫描↓、排序/回表如何变 | 判断 SQL 层 |
| AHI on/off | `btr_search` vs B+Tree 比较 | 判断 AHI 收益 |
| `sync_binlog` 1→0（仅测试） | fsync/组提交是否下降 | 判断写瓶颈 |
| 关闭 order/distinct | collation 热点是否消失 | 判断 query-mix |
| Buffer Pool 增大 | 磁盘读/页替换是否下降 | 判断工作集 |
| 绑核/IRQ 调整 | 跨 NUMA/IRQ 抢核是否缓解 | 判断系统层 |

每次只改一个变量，记录热点迁移与性能数字，避免“改了多处无法归因”。
