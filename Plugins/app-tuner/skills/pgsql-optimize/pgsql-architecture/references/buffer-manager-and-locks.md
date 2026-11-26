# 缓冲区管理器与锁体系（REL_17_5）

> 只读知识库。源码锚点 REL_17_5；本文件的“HNSW 场景”描述以 pgvector 实测口径
> （数据量 80GB、预热后全命中）为例说明锁路径，pgvector 版本以目标环境
> `SELECT extversion FROM pg_extension WHERE extname='vector'` 为准。

## 1. ReadBuffer 全路径（页已在池中时）

```
ReadBuffer(rel, blkno)
  → ReadBufferExtended → PinBufferForBlock
      → BufferAlloc(smgr, fork, blkno)          bufmgr.c
          1. BufTableHashCode(&newTag)           hash_any，~20ns 量级
          2. BufMappingPartitionLock(newHash)    分区 = hash % NUM_BUFFER_PARTITIONS
          3. LWLockAcquire(分区锁, LW_SHARED)
          4. BufTableLookup(&newTag)             共享 dynahash 查 BufferTag→BufferDesc
          5. found → PinBuffer(buf)              CAS：refcount+1、usagecount 提升
          6. LWLockRelease(分区锁) → return buf  （未 found 分支才走 I/O/淘汰）
```

- 每次访问一个页（点查回表、索引下降、向量页）都完整走这条链：**热点场景 =
  分区锁竞争 + PinBuffer CAS + BufferDesc 数组随机访问**。
- 未 found 分支：`StrategyGetBuffer`（clock-sweep 选 victim，可能命中脏页走
  FlushBuffer）→ 读盘 → 插入哈希表（需独占分区锁）。
- 对应等待事件：`BufferMapping`（等分区锁）、`BufferContent`（等内容锁）、
  `BufferPin`、`BufferIO`——采样口径见
  `../../pgsql-hotspot-analysis/references/profiling-playbook.md`。

## 2. NUM_BUFFER_PARTITIONS（BufMappingLock 分区数）

- 定义：`src/include/storage/lwlock.h`（编译期常量，上游默认 128 亦随大版本调整，
  以目标版本源码为准；修改需重编，见
  `../../pgsql-performance-optimization/references/buffer-and-lock-optimizations.md`）。
- 效果与竞争形态要分层看：哈希**查找**（BufTableLookup）持 `LW_SHARED`——两个
  backend 的页面哈希落同分区时**可以同时持有**共享锁，仅凭哈希碰撞并不能推断它们
  互相阻塞；真正的串行点在**插入/删除映射**（BufTableInsert/BufTableDelete 需
  `LW_EXCLUSIVE`）与锁状态字上的原子操作开销（即使无阻塞，同分区的原子
  read-modify-write 也会在缓存行上排队）。高并发随机访问（如 HNSW 图遍历）下
  三者叠加成为可测热点。
- 判别（每种证据对应一种竞争）：等待事件 `LWLock/BufferMapping` 验证的是**真
  阻塞**——共享查找碰撞但无等待则不产生该事件，独占插入/删除被挡才累计；原子操作
  开销不产生等待事件、只表现为 CPU 消耗（on-CPU 火焰图中原子指令相关帧变宽）；
  阻塞等待时间本身不进 on-CPU 栈（off-CPU 时间，须等待事件或 off-CPU 采集）。

## 3. dynahash（BufTableLookup 的数据结构）

```
HTAB（共享 dynahash，BufTable 即一个 HTAB）
  ├─ Directory        指针数组（按需扩容），每项指向一个 Segment
  ├─ Segment[0..N]    段数组：固定容量桶的块
  │    └─ bucket      桶链头 → HASHELEMENT（链入元素，含 hashvalue/next）
  └─ 元素             BufferTag 哈希条目（key:tag → value:buf_id）
```

- `nelem` 由创建时按 NBuffers+冗余估算（`ShmemInitHash`，`src/backend/utils/hash/dynahash.c`）：
  shared_buffers 越大，哈希表条目/Directory/Segment 越大，**随机访问的元数据
  工作集也越大**——这是“缓冲池不是越大越好”的机制根源之一。
- 查询路径 `hash_search_with_hash_value`：Directory 取段 → 段内取桶 → 链上比对
  hashvalue/key。桶链过长（负载因子）会拉长 `BufTableLookup`——扩分区数不改
  链长，改的是“等谁”。
- 与 `NUM_LOCK_PARTITIONS`（Lock Hash Table 分区）是**两套独立分区**：前者管
  缓冲区映射（BufMappingLock），后者管重量级锁条目（见 §4），数值与用途都不同。

## 4. LWLock 体系与 Lock Hash Table

### 4.1 LWLock 三类锁位

| 锁位 | 位置 | 谁竞争 | 实测口径（wo 负载，占 postgres CPU） |
|------|------|--------|-----------------------------------|
| 目标锁本体 | WALInsertLock/WALWriteLock/分区锁等 | 按功能 | XLogInsertRecord 24.3 / XLogFlush 25.3 |
| 等待队列自旋锁 `LWLockWaitListLock` | lwlock.c，排队/唤醒先抢 | **所有等/醒进程** | 32.3（第一大单项；帧内含原子检查与短暂自旋，长等待走信号量睡眠） |
| buffer 头自旋 `LockBufHdr` | buf_internals.h 状态字 CAS | 同页多进程 | rw 下随淘汰链上升 |

- 机制：进程在目标锁上睡眠/被唤醒，都要先抢目标锁的**等待队列自旋锁**再改队列；
  高频提交场景（如 240 backend 短事务）下这行缓存字跨核乒乓——futex 睡/醒 +
  `try_to_wake_up` 另计 ~8-10%。
- PMU 证据形态：写负载 IPC 0.233、Retiring 2.9%、原子/屏障串行化 33.2%、
  L3_Bound ~50%——“锁自旋烧 CPU”的微架构直接证据（口径：topdown，ARM PMU，
  PG 17.5 wo 负载）。

### 4.2 重量级锁（Lock Hash Table）

- LOCK（锁条目：locktag+ granted/wait 队列）与 PROCLOCK（进程↔锁 关联）都是共享
  dynahash 条目；按 locktag 哈希分到 `NUM_LOCK_PARTITIONS`
  （=1<<`LOG2_NUM_LOCK_PARTITIONS`，`src/include/storage/lock.h`）个分区锁。
- 表锁/页锁等**关系级**条目在同一张表管理；普通**行锁**不是这里的条目——它记在
  堆元组头（xmax），`SELECT FOR UPDATE` 等待并发事务时表现为等待对方**事务 ID**
  （`pg_stat_activity` 常见 XactLock 等待），共享锁表里找不到“行锁记录”。fastpath
  只处理特定弱模式的**关系锁**（`LockAcquireExtended` 先查进程私有
  `LockMethodLocalHash` 与 fastpath 数组，弱冲突 acquisition 不碰共享表），冲突升级
  才进共享表排队——不要按“去共享锁表逐行找持锁记录”排查行锁。
- 场景（pgvector HNSW）：扫描期 `LockPage` 类重量级锁请求走 Lock Hash Table
  分区锁；与 BufMappingLock 分区（§2/§3）无关——排查时先分清“哪张哈希表的
  哪个分区”。
- 等待事件：重量级锁在 `pg_stat_activity` 里通常表现为 `Lock` 类（locktype
  细分），与 LWLock 类分开统计。

## 5. PinBuffer 的 CAS 语义

- `PinBuffer`（bufmgr.c）对 BufferDesc state 字做 `pg_atomic_compare_exchange_u32`
  循环：refcount/usagecount 打包在同一字——**同页多进程 pin/unpin 会 CAS 重试**。
- 采样口径限制：CAS 失败后的**重试循环仍在 CPU 上执行**，会进入 on-CPU 采样——
  但普通火焰图无法仅凭函数帧区分成功次数与失败次数（同一帧两种结果都采样到）；
  要把「重试占比」变成可判读的结论需要**重试计数器**（自研 pg_buffer_stats 的
  cas_hotspots 视图按 CAS 返回值分桶计数，见
  `../../pgsql-test-verification/references/observation-extensions.md`）。
- 与 buffer 头自旋（§4.1）区分：LockBufHdr 改 state 的标志位段，PinBuffer 改
  refcount 段——同字不同段，同 CAS 机制。
