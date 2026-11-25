# WAL、MVCC 与提交链路（REL_17_5）

> 只读知识库。百分比口径：PostgreSQL 17.5、sysbench oltp_write_only（每事务
> 2×索引 UPDATE + 1×非索引 UPDATE + 1×DELETE + 1×INSERT）、ARM aarch64 240 核；
> 占 postgres 进程 CPU，folded 全栈精确拆分。

## 1. 写一条 DML 的链路

```
ExecModifyTable
  ├─ ExecUpdate/ExecInsert/ExecDelete（heapam）
  │    ├─ heap_page_prune（UPDATE 路径页满时清 HOT 死链，实测 6.5%）
  │    └─ ExecInsertIndexTuples → _bt_doinsert（次级索引完整下降，实测 13.7%）
  │       （btree：沿途锁 buffer、_bt_compare；页满则页分裂 + bottom-up 删除）
  └─ WAL 组装 XLogInsertRecord（实测 24.3%）
       → WALInsertLock 分片插入 WAL buffer
       → 组页/满页时 WALWriteLock 写出
提交 CommitTransactionCommand（实测 28.8%）
  → XLogFlush（实测 25.3%，其中 WaitXLogInsertionsToFinish 22.3%——检查+自旋+睡眠混合，见下注）
  → 事务锁清理/proc 数组剔除 → 应答客户端
```

- `WaitXLogInsertionsToFinish`：提交者等**所有先前插入者**把记录越过所需 LSN——
  两段机制：先原子读取插入进度（insertingAt 变量，可短暂重试），仍有冲突则经
  `LWLockWaitForVar` 在信号量上**睡眠**等待（17.5 源码）。火焰图上出现该帧时
  既有 CPU 上的检查/自旋也有醒后路径——把整段时间都算“自旋烧 CPU”会高估。
- 次级索引无 change buffer（对照 InnoDB）：每条索引列变更当场完整下降；sysbench
  每表 1 个次级索引已 13.7%，索引越多放大越多。

## 2. 提交路径的锁放大

- 每个进程排队/唤醒 WALInsertLock/WALWriteLock 都要抢 `LWLockWaitListLock`
  （见 `references/buffer-manager-and-locks.md` §4.1）——240 进程高频提交时它以
  32.3% 成为写负载第一大单项，且 futex 睡/醒 + try_to_wake_up 另计。
- GUC 影响面（真实存在的参数，处置归 pgsql-performance-optimization）：
  `synchronous_commit`（off=提交不等本地刷盘；local=仍等本地刷盘、仅省同步备库确认）、`commit_delay`/`commit_siblings`
  （组提交：主动延迟换批量刷）、`wal_buffers`、`wal_writer_delay`、
  `wal_compression`（WAL 体积换 CPU）。
- 判别顺序：先 `pg_stat_activity` 看 `WALSync`/`WALWrite` 等待占比，再决定动
  提交参数还是存储（fsync 能力）。

## 3. MVCC 与快照

- `GetSnapshotData`（procarray.c）：顺序扫 ProcArray 组活跃事务快照——连接数
  线性（实测 ro 3.8 / rw 5.9 / wo 3.0%，240 backend 口径）；高并发短事务负载
  里是固定税。
- 读可见性：元组头的 hint bits 让已提交判断免查 CLOG（点查负载
  `XidInMVCCSnapshot` ~0.1%）；批量扫描大量未 hint 的元组才会走
  `TransactionIdIsInProgress`/CLOG 路径。
- 死元组与 HOT：UPDATE 不改索引列时可走 HOT（同页内链更新，索引不新增条目）；
  页满时的 `heap_page_prune` 在**用户路径**清 HOT 死链——写负载 6.5% 的来源；
  大面积死亡由 autovacuum 兜底（autovacuum_* 调参归 performance skill）。

## 4. 缓冲区刷脏与 checkpoint 的交互

- 读路径被迫刷脏的条件与代价（见
  `references/overview-and-process-model.md` §4）：bgwriter/checkpointer 消化
  不及时 → clock-sweep 命中脏 victim → 查询进程同步 FlushBuffer + 排 fsync 队列
  （rw 实测：GetVictimBuffer ~15.7 + FlushBuffer/mdwritev 11.7 +
  ForwardSyncRequest 8.3%）。
- `ForwardSyncRequest` 把 fsync 请求转发给 checkpointer 做合并；checkpointer
  无法接收（未运行/无法吸收）时该函数返回失败、**进程在关键路径自查 fsync**
  ——吸收不了的 fsync 永远在业务进程上。
- checkpoint 风暴：checkpoint_completion_target 低 → 后期集中刷脏 → 与业务写
  撞车；判别用 `pg_stat_checkpointer`（PG 17 起检查点计数从 pg_stat_bgwriter 迁到
  此视图：`num_requested`/`num_timed`/`buffers_written` 的同窗口增量）与 IO 等待事件，
  bgwriter 侧看 `buffers_clean`/`maxwritten_clean`。

## 5. GUC 速查（本域涉及，均真实存在）

| GUC | 影响位置 | 机制一句话 |
|-----|---------|-----------|
| shared_buffers | NBuffers、BufferDescriptors/BufTable 大小 | 元数据工作集与池命中率的权衡 |
| wal_buffers / wal_writer_delay | WAL buffer 深度与提前刷 | 插入满页等待的概率 |
| synchronous_commit | XLogFlush 等待 | off：提交延迟 vs 持久性窗口（崩溃丢最近提交）；local：仅省同步复制确认，无同步备库时收益≈0 |
| commit_delay / commit_siblings | 组提交 | 提交排队批量刷 |
| bgwriter_delay / bgwriter_lru_maxpages / bgwriter_lru_multiplier | 后台刷节奏 | 查询进程被迫刷脏的概率 |
| checkpoint_completion_target | checkpoint 摊平 | 脏页集中度 |
| autovacuum_* | 死元组回收 | heap_page_prune/扫描膨胀 |
| plan_cache_mode | GetCachedPlan 路径 | generic/custom plan 选择 |
