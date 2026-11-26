# PostgreSQL 进程与内存模型（REL_17_5 / REL_18_3）

> 只读知识库。源码锚点：官方 tag `REL_17_5` / `REL_18_3`；行号差异以 17.5 为基准、
> 显式标注 18 变化。

## 1. 一连接一进程

```
postmaster（监听 5432，每连接 fork 一个 backend）
  ├─ backend ×N        客户端会话进程（PostgresMain 循环）
  ├─ bgwriter          批量写脏页（src/backend/postmaster/bgwriter.c）
  ├─ checkpointer      checkpoint：脏页刷盘 + fsync 合并（checkpointer.c）
  ├─ walwriter         提前把 WAL buffer 写出（walwriter.c）
  ├─ autovacuum launcher/worker    清理死元组（autovacuum.c）
  └─ walsender 等      流复制/逻辑解码
```

- postmaster `fork()` 出 backend。退出形态要分开说：**单会话正常退出**（客户端断开/
  `\q` 退出）只结束本 backend，其他连接与实例不动；**backend 崩溃**（段错误等）
  时 postmaster 会终止**其余全部** backend 并做崩溃恢复——其他连接的在途事务也被
  中断（不是“只影响本连接”）；**实例关闭**（`pg_ctl stop`，目标是整套实例）是另一
  类操作——默认 fast 模式会断开**所有**连接并回滚未提交事务，不能与单会话退出混用。
- 与线程模型（MySQL）的根本差异：**无线程级共享堆**，跨进程协作全部经共享内存 +
  LWLock/信号量；代价是连接数高时进程切换与内存开销上升（每 backend 私有内存上下文）。

## 2. fork 与共享内存语义（常见误读修正）

| 问题 | 正解 |
|------|------|
| 子进程的 `static` 变量（`MyProc`、`LockMethodLocalHash`）是共享的吗 | 变量本身（指针/表头）是每个进程私有的一份副本；**指向的数据**是否共享取决于它指向哪里 |
| `MyProc` | 指向共享段 ProcGlobal 数组的槽位（`src/backend/storage/lmgr/proc.c`）——每个 backend 指针值不同，数据在共享内存，锁等待链经它串起来 |
| `LockMethodLocalHash` | 每进程私有的本地哈希表（记录本进程持有的锁），配 fastpath 数组使用；真正跨进程的 LOCK/PROCLOCK 条目在共享 Lock Hash Table（见 `references/buffer-manager-and-locks.md` §4） |
| fork 会复制共享段吗 | 不会复制内容。共享段由 postmaster 启动时 `PGSharedMemoryCreate`（`src/backend/storage/ipc/shmem.c`）创建，fork 后子进程继承**映射**；匿名私有内存（栈/堆）才是写时复制 |
| 共享段与私有内存怎么区分 | 共享分配走 `ShmemInitHash`/`ShmemInitStruct`（登记在 shmem index）；backend 私有走内存上下文（`src/backend/utils/mmgr/`，AllocSet 等） |

## 3. 共享内存三件套

| 结构 | 创建入口 | 大小口径 | 保护 |
|------|---------|---------|------|
| 缓冲池（BufferDescriptors + buffers） | `CreateSharedMemoryAndSemaphores`→`InitBufferPool` | NBuffers = shared_buffers/8KB；每 BufferDesc 约 64B（tag/state/buf_id/freeNext） | BufMappingLock 分区 + buffer 头自旋 + 内容锁 |
| WAL buffer | `XLOGShmemInit` | wal_buffers：默认 -1 → 取 shared_buffers/32，下限 64KB、**上限=一个完整 WAL 段**（默认段 16MB 时为 16MB；段 64MB 时可达 64MB） | WALInsertLock 分片 + WALWriteLock |
| Lock Hash Table / ProcGlobal 等 | `LockShmemInit`、`ProcGlobalInit` | max_locks_per_transaction 口径 | NUM_LOCK_PARTITIONS 分区锁 |

- 动态哈希表（dynahash）是共享结构的标准载体：HTAB → Directory（指针数组）→
  Segment（段数组）→ 元素；`nelem` 由创建时估算（如 NBuffers+冗余），详见
  `references/buffer-manager-and-locks.md` §3。
- `shared_preload_libraries` 的扩展（pg_stat_statements 等）在 postmaster 阶段申请
  共享内存——**装这类扩展必须重启实例**，`shared_preload_libraries` 改完 reload 不生效。

## 4. 后台进程与查询进程的分工（为什么查询进程会自己刷脏）

正常分工：bgwriter 按针扫策略提前刷脏；checkpointer 在 checkpoint 时把脏页集中写盘并
合并 fsync；查询进程只读不写。**消化不及时时**（写速率 > 后台刷速率，或
bgwriter_* 参数保守）：

```
查询进程 clock-sweep 选 victim → GetVictimBuffer 命中脏页
  → FlushBuffer → mdwritev（同步写盘）→ ForwardSyncRequest（排 fsync 队列，超限则自查 fsync）
```

即“读查询在关键路径替后台打工”。实测口径（sysbench oltp_read_write，PG 17.5，
ARM 240 核，占 postgres CPU，inclusive 嵌套——子项按链路递进包含，不可相加）：
StartReadBuffer 24.8% → clock-sweep 淘汰 GetVictimBuffer ~15.7 → 命中脏页
FlushBuffer/mdwritev 11.7 → 排 fsync 队列 ForwardSyncRequest 8.3。调参方向见
`pgsql-performance-optimization`（bgwriter 节奏/占位成本权衡，非本 skill 职责）。

## 5. 内存上下文（backend 私有）

- 每条语句的解析树/计划/执行器状态分配在 Portal/Executor 上下文，语句结束整体释放
  ——避免逐对象 free；长连接的缓存（plan cache、catcache）在专属上下文存活。
- 分配热点：`AllocSetAlloc` 在点查负载占比 <1%（实测），不是常见瓶颈；真要出问题
  先看大结果集/排序溢出（`work_mem` 口径）。

## 6. 每连接成本速查

| 项 | 口径 |
|----|------|
| 进程内存（空闲连接） | 数 MB 级（上下文+catcache 预热后增长）；高并发短连接场景用连接池（pgbouncer 等事务级池）替代 |
| 上下文切换 | 一来一回的短查询高频 sleep/wake（futex）；与线程模型对比是 PG 的结构性开销 |
| 锁开销 | GetSnapshotData（ProcArray 顺序扫）、轻锁获取的原子指令；连接数↑→共享行竞争↑ |
