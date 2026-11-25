# 竞争机制映射（症状 → 机制 → 证据）

> 机制详述在 `../../pgsql-architecture/references/buffer-manager-and-locks.md` 与
> `../../pgsql-architecture/references/wal-mvcc-and-commit.md`（名词同口径）；本页是归因速查。

## 1. 六大机制

| # | 机制 | 函数链/锁 | 直接证据 | 典型负载 |
|---|------|----------|---------|---------|
| 1 | WAL 提交串行点 | XLogFlush→WaitXLogInsertionsToFinish（等先前插入完成——**三种形态**：读插入指针的进度检查、经 LWLockWaitForVar 的阻塞睡眠、真正的忙自旋） | 火焰图 WaitXLogInsertionsToFinish 帧（含睡眠路径的调用方，不能仅凭帧高断言忙自旋）；等待事件 WALWrite/WALSync 只证明在等**刷盘**（write/fsync 类），不能证明在等插入者 | wo（25.3%/22.3%） |
| 2 | 等待队列自旋锁 | 排队/唤醒先抢 LWLockWaitListLock（lwlock.c） | 火焰图 LWLockWaitListLock；topdown 串行化 33.2%；futex+try_to_wake_up ~8-10% | wo（32.3%，第一大项） |
| 3 | buffer 淘汰刷脏链 | GetVictimBuffer→FlushBuffer→mdwritev→ForwardSyncRequest | rw 火焰图链路；等待事件 BufferIO/IO.DataFileWrite；pg_stat_bgwriter 计数消化滞后 | rw（24.8 内嵌 15.7/11.7/8.3） |
| 4 | BufMapping 分区竞争 | BufferAlloc→BufMappingPartitionLock(LW_SHARED)→BufTableLookup | 等待事件 LWLock/BufferMapping；自研扩展 cas_hotspots（CAS 重试） | 高并发随机读（HNSW 遍历等） |
| 5 | 网络 epoll 代价 | pq_recvbuf→recv EAGAIN→WaitEventSetWait→epoll_pwait | 火焰图 epoll_pwait（与命令频率成正比：ro 6.4>rw 4.8>wo 2.9） | 短查询高频往返 |
| 6 | 计划重建 | GetCachedPlan→BuildCachedPlan→standard_planner | 火焰图 BuildCachedPlan；pg_stat_statements Δplans/Δcalls（仅整个窗口启用 track_planning 且 calls 有增量时可判读——该 GUC 默认 off，plans=0 不代表无重建，标“不可判读”） | ro（读多写少侧） |

## 2. “火焰图正常但吞吐差”的排查顺序

1. **锁等待先分形态**：阻塞睡眠不进 on-CPU 栈（等锁排队类先看等待事件采样与
   offcputime）；忙自旋/CAS 轮询仍在 CPU 上、可见于 on-CPU 火焰图（§1 表中
   LWLockWaitListLock/WaitXLogInsertionsToFinish 的高占比帧是**候选证据**——
   前者基本是自旋/队列开销，后者还可能是「睡眠路径的调用方帧」：PG 17.5 起
   WaitXLogInsertionsToFinish 在前序插入未完成时可经 LWLockWaitForVar 睡眠，
   该睡眠时间不占 CPU，须配等待事件/off-CPU 才能分清）；
2. **off-CPU**：offcputime 看“睡眠原因链”（锁/IO/信号量等待的时长维度）；
3. **客户端瓶颈**：sysbench 端 CPU、网络往返（TX>RX 且内核协议栈占大头时，
   先分清“回传结果行”与“纯等待”）；
4. **观测自身**：pgss_store ~1.8-5%——确认口径再下结论。

## 3. 机制→证据的三方互证示例

- 判“锁自旋主导”（wo 形态）需要同时满足：①火焰图 LWLockWaitListLock/
  WaitXLogInsertionsToFinish 高（WaitXLog 帧先排拆睡眠路径：同窗口
  WALWrite/WALSync 等待事件高则部分时长是刷盘/睡眠而非自旋）；②topdown
  原子串行化 >30%、Retiring <5%、L3_Bound ~50%；③等待采样 LWLock/WAL 类
  占比高——三方齐才归因，缺一即降级为“假设待证”；
- 判“淘汰刷脏链主导”（rw 形态）：①火焰图链路三连帧；②等待事件 BufferIO/
  DataFileWrite；③pg_buffercache 脏页比例 + pg_stat_bgwriter 消化计数。

## 4. 与优化侧的接口

每条机制的处置入口（本 skill 只归因不开方）：

| 机制 | 处置入口 |
|------|---------|
| WAL 提交自旋 | `../../pgsql-performance-optimization/references/wal-and-plan-optimizations.md`（commit_delay/synchronous_commit 等） |
| 等待队列自旋锁 | 同上（组提交/连接数规划）+ `buffer-and-lock-optimizations.md`（LWLOCK 侧） |
| 淘汰刷脏链 | 同上（bgwriter/checkpoint 组） |
| BufMapping 分区 | `../../pgsql-performance-optimization/references/buffer-and-lock-optimizations.md`（NUM_BUFFER_PARTITIONS/shared_buffers） |
| 网络 epoll | 连接池/往返合并（协议层，域外）；确认后如实标注“结构性成本” |
| 计划重建 | `../../pgsql-performance-optimization/references/wal-and-plan-optimizations.md`（plan_cache_mode 等） |
