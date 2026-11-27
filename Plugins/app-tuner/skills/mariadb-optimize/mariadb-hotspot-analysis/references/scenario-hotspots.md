# 场景热点与瓶颈迁移

本文件按负载场景给出“常见热点 → 机制 → 证据 → 方向”。数值来自历史实测，只作假设起点。

## 1. 只读（sysbench oltp_read_only）

**特征**：多语句只读事务，包含点查、范围扫描、聚合；buffer pool 命中决定上限。

| 热点 | 机制 | 证据 | 方向 |
| --- | --- | --- | --- |
| B+tree 查找 | 页内二分 + 记录比较 | `perf` 中 `btr_cur_search_to_nth_level` | 覆盖索引、减少回表 |
| 二级索引回表 | 二级索引→聚簇索引二次查找 | `Handler_read_rnd_next` 高、执行计划 | 联合索引/覆盖索引 |
| buffer pool hash/LRU | 页查找与链表移动 | `buf_pool` 相关函数、`%sys` 高 | 增大 pool、分实例 |
| 内存分配 | 行/临时对象 | `malloc/free`、`memcpy` | 减少临时表、调分配器（需验证） |
| latch | 页 latch/索引 latch | `futex`、off-CPU 在 `rw_lock` | 减少热点页并发、分片 |

注意：只读 QPS 高时，`Handler_read_*` 与 `Innodb_buffer_pool_read_requests` 是逻辑读
规模；物理读（`Innodb_buffer_pool_reads`）高说明工作集超出缓存。

## 2. 点查（oltp_point_select）

**特征**：单行主键/唯一索引，QPS 极高，单次工作量极小。

| 热点 | 机制 | 证据 | 方向 |
| --- | --- | --- | --- |
| 线程调度/上下文切换 | 每连接一线程或线程池排队 | `cs` 高、`%sys`、futex | 线程池、绑核、减少连接 |
| 页 hash/LRU | 共享结构竞争 | `buf_pool` latch、off-CPU | 多 buffer pool 实例 |
| MDL/表缓存 | 打开表、元数据锁 | `Table_open_cache_misses`、MDL 等待 | 增大 table_open_cache |
| 网络/协议 | 小包高频 | `sar -n DEV`、软中断 | 批量、连接复用、绑中断 |

点查常见误判：把“线程调度开销”当成“SQL 慢”；此时优化 SQL 无用，应调线程模型/绑核。

## 3. 只写（oltp_write_only / oltp_insert）

**特征**：插入/更新/删除混合，CPU 常不饱和，延迟受日志与刷盘影响。

| 热点 | 机制 | 证据 | 方向 |
| --- | --- | --- | --- |
| redo 写入/刷盘 | log buffer → log file，fsync | `Innodb_log_waits`、`log_writes`（10.11 无单独日志 fsync 计数器，fsync 总数看 `os_data_fsyncs`）、`%sys` | 增大 redo、组提交、分盘 |
| doublewrite | 脏页先写双写区 | `iostat` 写量、`buf_dblwr` | NVMe 可评估关闭（风险） |
| 刷脏/checkpoint | page cleaner 跟不上 | dirty pages、pending writes | 提高 io_capacity（10.11 无 page_cleaners 参数） |
| binlog 组提交 | 事务提交写 binlog | `binlog_commits`、组提交触发计数 | `binlog_commit_wait_*`、`sync_binlog` |
| 页分裂 | 随机插入导致分裂 | `Innodb_pages_created`、写放大 | 有序主键、批量插入 |
| undo/purge | 更新产生 undo | `History list length` | 拆事务、调 purge |

纪律：写场景先确认持久性要求（`innodb_flush_log_at_trx_commit`/`sync_binlog`），
再谈性能；不要为了 TPS 静默降低持久性。

## 4. 非索引更新（sysbench oltp_update_non_index）

**特征**：该 lua 的实际语句是 `UPDATE sbtestX SET c=? WHERE id=?`——`id` 是主键，
默认走主键点查；“non_index”指被更新的 `c` 列没有索引，并不是 WHERE 条件无索引。
不能据本节把正常负载误诊成全表扫描并建议给 `c` 加索引。真正无索引 WHERE
（如 `WHERE c=?`）单独列在表末，只有那种语句才走全表扫描。

| 热点 | 机制 | 证据 | 方向 |
| --- | --- | --- | --- |
| 热点行行锁 | 主键点查集中在少数行，RR 下记录锁/死锁 | `INNODB_LOCK_WAITS`、`SHOW ENGINE INNODB STATUS`、perf 中 `lock_sys` | 降冲突/拆批/评估 RC（谨慎） |
| redo 写入/刷盘 | 每行更新产生 redo | `Innodb_log_waits`、`log_writes`（fsync 总数看 `os_data_fsyncs`）、IO await | redo/io_capacity/分盘 |
| undo 膨胀 | 大量行更新产生 undo | history list、undo 表空间 | 拆批、及时提交 |
| purge 滞后 | 长事务持有 read view | `trx_rseg_history_len` | 缩短事务、避免长读 |
| 无索引 WHERE（`WHERE c=?`，非默认 lua） | 条件列无索引才全表扫描 + next-key 锁 | `Handler_read_rnd_next`、`EXPLAIN`、`INNODB_LOCK_WAITS` | 加索引/改条件（注意写放大）、缩小范围 |

## 5. TPC-C（C3 类混合）

**特征**：TPC-C 含五类事务混合读写；按 TPC-C 规范事务 mix 10:10:1:1:1，按事务数
New-Order/Payment 各约 43% 是最热档，Order-Status/Delivery/Stock-Level 各约 4%，高并发下
锁竞争与日志是主线。实际测试方案若为自定义占比，按 `mariadb-test-verification` 的固定口径
单独记录来源与数值，不与规范档位混排。

| 热点 | 机制 | 证据 | 方向 |
| --- | --- | --- | --- |
| 行锁/hash 抢锁 | 热点行并发更新 | 锁等待、`lock_sys` 在 perf 中 | 降低冲突（分片/业务）、减少持锁时间 |
| 原子操作 | 引用计数/状态更新 | `cas`/`ldadd` 在 perf 顶部 | LSE（ARM）、减少共享写、padding |
| 跨 NUMA 访问 | 线程/内存跨片 | `numastat`、远端访问 | 绑核 + membind |
| redo/刷脏 | 写密集 | `Innodb_log_waits`、IO await | redo/io_capacity/分盘 |
| 调度迁移 | 线程在核间迁移 | `cs`、`psr` 变化 | 固定绑核、调度域策略（需内核支持） |

> 历史证据：鲲鹏 4P 的 C3 场景中，“hash_lock 抢锁 + CAS 原子操作”是主要瓶颈，
> 顺序锁、拆分 lock_word、LSE、调度域优先前 2P 等手段有实测收益。**这些是 10.3 的
> 结论**；10.11 的 `rw_lock` 已是 `std::atomic` 实现，必须重新采样确认热点是否仍成立。

## 6. 瓶颈迁移规律

```
低并发 → 高并发：
  单查询成本 → 线程调度/锁/原子 → 缓存与内存带宽 → IO/日志

点查 → 范围读：
  futex/线程 → B+tree/回表 → buffer pool 容量

只读 → 只写：
  CPU 计算 → redo/刷盘/双写 → IO 饱和、P99 抖动

单实例 → 多实例/多 NUMA：
  本地争用 → 跨 NUMA 远端访问 → 绑核与内存亲和
```

## 7. x86 与 ARM 差异

| 维度 | ARM（鲲鹏） | x86 |
| --- | --- | --- |
| 原子指令 | ARMv8.0 用 LDREX/STREX 循环；8.1+ 有 LSE | 原生 `lock` 前缀/CAS |
| LSE 收益 | 竞争激烈时明显，但部分场景会退化 | 不适用 |
| NUMA | 多 Die/多 NUMA，跨片代价大 | 视平台 |
| 指令计数 | `cas/ldadd/swp` 可统计 | `lock cmpxchg` 等 |
| 编译器 | GCC 对 `-march=armv8.2-a+lse` 支持需版本 | `-march=native` |

## 8. 扩展实验（用于验证归因）

| 实验 | 预期观察 | 说明 |
| --- | --- | --- |
| 并发 64→128→256 | 找到 QPS 拐点与 CPU 饱和点 | 区分容量 vs 争用 |
| 加覆盖索引 | 逻辑读下降、回表减少 | 验证回表假设 |
| 增大 buffer pool | 物理读下降 | 验证缓存假设 |
| 增大 redo | `Innodb_log_waits` 下降、写 P99 改善 | 验证日志假设 |
| 固定绑核 | `cs` 下降、远端 NUMA 下降 | 验证调度/跨片假设 |
| 关闭 AHI | latch 竞争下降或 QPS 下降 | 验证 AHI 假设（需实测） |
| 只读 vs 只写 | 热点从 CPU 转向 IO/日志 | 验证场景差异 |

每个实验只改一个变量，并保留对照数字；否则无法归因。
