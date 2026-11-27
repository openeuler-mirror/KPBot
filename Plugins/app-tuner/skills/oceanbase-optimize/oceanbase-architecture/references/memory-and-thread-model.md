# 内存与线程模型

源码根 `${OB_SRC}`；路径相对该根。内存细节可同时参考 `${OB_SRC}/docs/memory.md`
（源码树自带文档）与本文。

## 1. 五层内存结构

```
Layer 5 业务分配器    ob_malloc / ObMalloc / ObArenaAllocator / ObFifoArena / ObMemstoreAllocator
Layer 4 租户×Ctx×NUMA ObTenantCtxAllocator(V2)（ObjectMgr/ObjectSetV2/BlockSet）
Layer 3 租户限制      ObTenantMemoryMgr（hold_bytes_[ctx] / limit_bytes_[ctx]）
Layer 2 全局块管理    AChunkMgr（slots_[NUMA][size_class] 自由链表，mmap 2MB 对齐）
Layer 1 OS            mmap/munmap
```

| 结构 | 大小 | 位置 | 说明 |
| --- | --- | --- | --- |
| `AChunk` | 2MB 对齐（`INTACT_ACHUNK_SIZE`） | `deps/oblib/src/lib/alloc/alloc_struct.h` | 物理内存基础单元，含 NUMA 归属、位图 |
| `ABlock` | 8KB（`ABLOCK_SIZE`） | 同上 | Chunk 内逻辑块 |
| `AObject` | 头 48B（`AOBJECT_HEADER_SIZE`），另有 16B 尾魔数（`AOBJECT_META_SIZE=64`） | 同上 | 每次分配的头，含 label/大小 |
| `ObMemAttr` | — | 同上 | tenant_id/ctx_id/label/numa_id/prio |

### 1.1 分配器谱系

```
ObIAllocator (allocator/ob_allocator.h)
 ├─ ObMalloc                 → 包装 ObMemAttr，转 ObMallocAllocator 单例
 ├─ ObAllocator(v2)          → ObjectSet + 可选 PageManager
 ├─ ObParallelAllocator      → ≤8 通道，降锁争用
 ├─ ObArenaAllocator         → bump 分配，reset()/clear() 批量释放（查询临时内存）
 ├─ ObFifoArena              → FIFO 页，按页退休（MemStore）
 └─ ObMemstoreAllocator      → ObFifoArena + throttle + per-memtable handle
```

### 1.2 Context ID（性能归因维度）

定义于 `deps/oblib/src/lib/allocator/ob_mod_define.h`。常用：

| Context | 用途 | 超限行为 |
| --- | --- | --- |
| `MEMSTORE_CTX_ID` | MemTable 写入 | 触发冻结/写限流 |
| `EXECUTE_CTX_ID` | SQL 执行临时内存 | 查询失败/落盘 |
| `PLAN_CACHE_CTX_ID` | 计划缓存 | 淘汰计划 |
| `KVSTORE_CACHE_ID` | block/row/bloom cache | 洗出缓存 |
| `TRANS_CTX_MGR_ID` | 事务上下文 | 事务失败/排队 |
| `WORK_AREA` | 排序/哈希工作区 | dump 到磁盘 |
| `MERGE_RESERVE_CTX_ID` | 合并缓冲 | 合并延后 |

`ObTenantMemoryMgr`（`deps/oblib/src/lib/resource/ob_resource_mgr.h`）为每租户每 Context
维护 hold/limit；分配先查租户总量再查 ctx 限额。

### 1.3 KV Cache

全局单例 `ObKVGlobalCache`（`src/share/cache/ob_kv_storecache.h`），按“租户 × 缓存
类型”实例化；memblock 固定 2MB，LRU/LFU 双策略 + 评分衰减 + hazard pointer 安全回收。

| 缓存 | 内容 | 失效影响 |
| --- | --- | --- |
| `plan_cache` | 物理计划 | 软解析退化，CPU 升高 |
| `user_block_cache` | 数据微块 | 读 IO 增加 |
| `index_block_cache` | 索引微块 | 索引扫描变慢 |
| `user_row_cache` / `fuse_row_cache` | 行/融合行 | 点查变慢 |
| `bloom_filter_cache` | 布隆过滤器 | 无效 IO 增加 |
| `schema_cache` | schema 对象 | DDL 后短暂抖动 |

### 1.4 MemStore

`ObFifoArena`（`src/share/allocator/ob_fifo_arena.h`）：bump 分配、按页退休、≤32 路并发。
`ObMemstoreAllocator` 增加写限流与 active memstore 统计：

```
active = arena.allocated() - hlist.hazard()
```

冻结阈值：`active_memstore_used > freeze_trigger_percentage × memstore_limit`。

## 2. 内存回收三条路径

1. **Cache washing**：租户分配失败 → `sync_wash_mbs()` 洗出低分 memblock → 归还 CHUNK_MGR。
2. **Block washing**：空闲 block `madvise(MADV_DONTNEED)` 归还物理页，统计 `washed_size_`。
3. **Chunk washing**：CHUNK_MGR 空闲链表 `munmap` 归还 OS。

> 观察“内存高但没 OOM”时，先看 cache 命中率与 wash 频率，而不是直接调大 `memory_limit`。

## 3. 线程模型

observer 单进程多线程。线程由 TG（Thread Group）框架统一管理
（`deps/oblib/src/lib/thread/`，定义见 `src/share/ob_thread_define.h`）。

### 3.1 关键线程

| 线程/线程组 | 数量（实测/默认） | 职责 | 热点信号 |
| --- | --- | --- | --- |
| `sql_nio` | `net_thread_count`（0=max(6,CPU/8)；128U 实测调到 36） | 收包、协议解析、投递请求 | 收包/序列化占比高、单线程 <数值>% |
| `worker`（`T<tenant>_L0_G0` 等） | `min_worker_cnt() = 2 + max(1, min_cpu × cpu_quota_concurrency)`，上限 `max_worker_cnt()`，中间按阻塞/队列长度动态 token 扩缩 | 租户请求处理（SQL 执行） | 执行算子、排序、扫描、排队 |
| `T<tenant>_ApplySrv` | 每租户若干（实测 5） | 提交回调、推进可见性 | insert 场景接近 <数值>% |
| `T<tenant>_ReplaySrv` | 每租户若干（实测 8–17+） | 备副本日志回放 | 备节点 CPU/流量 |
| `DagWorker` / `LSFreeze`（`src/share/ob_thread_define.h` 独立 TG） | 线程池/队列 | 冻结、转储、合并、迁移等后台 DAG 任务 | 写场景周期性 QPS 下降 |
| PX Pool | `px_workers_per_cpu_quota` | 并行执行 | DTL 传输、并行扫描 |
| IO 线程 | 由 IO Manager 管理 | 异步读写、回调 | off-CPU 等 IO |
| Timer/DAG/Schema/Stat | 各 1+ | 定时、合并 DAG、schema 刷新 | 后台抖动 |

### 3.2 请求分发

```
网络 → ObSrvDeliver::deliver()（src/observer/ob_srv_deliver.h）
  ├─ RPC → deliver_rpc_request()（lease/ddl/其他队列）
  └─ MySQL → deliver_mysql_request()
        → ObMultiTenant::recv_request(tenant_id, req)
          → ObTenant::recv_request() → req_queue_.push(req, QQ/RQ 六档优先级)
            → worker 出队后在其上下文执行 ObMPQuery::process()（含 ObSql::stmt_query）
```

`ObTenant`（`src/observer/omt/ob_tenant.h`）关键成员：`req_queue_`
（`ObPriorityQueue2<1, QQ_MAX_PRIO-1, RQ_MAX_PRIO-QQ_MAX_PRIO, OB_MAX_NUMA_NUM>`，
即 QQ_HIGH/QQ_NORMAL/QQ_LOW + RQ_HIGH/RQ_NORMAL/RQ_LOW 六档，含 NUMA 多队列）、
`multi_level_queue_`（按请求嵌套层级分流的 10 级队列，对应 L2_G0..L9_G0 线程）、
`retry_queue_`、`group_map_`（资源组）、`workers_`、
`meta_lock_`（TCRWLock）、`lock_`（ObLDLatch，生命周期读写锁）。

### 3.3 线程交互五机制

1. 请求队列（生产者 sql_nio，消费者 worker；条件变量唤醒）。
2. 定时器任务（Freezer 每 2s 检查、KVCacheWash、PlanCacheEvict 等）。
3. DAG 任务流（冻结后 mini/minor/major merge 的依赖调度）。
4. 条件变量/回调（IO 完成、事务提交、PX DTL）。
5. 共享内存（MemTable 的 MVCC 行链表，读写无锁冲突）。

### 3.4 cgroup 与绑核

`src/share/ob_cgroup_ctrl.h`：`ObCgroupCtrl` 为租户/进程设置 cgroup。两种常见做法：

- **cgroup 绑 NUMA**：容器/进程启动即限定 CPU 与内存节点，避免“先全机分配、后绑核”的跨 P。
- **taskset/numactl 绑核**：裸机常用；`taskset` 只改 CPU 亲和，不改已分配内存。

> `taskset -c 0-31` 后仍可能跨 NUMA 访存，这是本系列 insert 性能问题的主因之一。

## 4. 线程数计算（4.3.5 OMT）

4.3.5 的 worker 数由 `ObTenant` 的 min/max 与动态 token 决定，不是 3.x 的
`cpu_quota × workers_per_cpu_quota / 10` 近似式：

```
min_worker_cnt() = 2 + max(1, unit_min_cpu × cpu_quota_concurrency)   # 下限（ob_tenant.cpp）
max_worker_cnt() = max(memory_size / 20 / (stack_size + 3.5M), 150)   # 上限（ob_tenant.cpp）
actual_workers   = min_worker_cnt() … max_worker_cnt() 之间按阻塞/队列长度动态扩缩 token
px_workers       ≈ cpu_quota × px_workers_per_cpu_quota
```

- `workers_per_cpu_quota` 仅在 `ob_multi_tenant.cpp` 作为构造参数 `times_of_workers`
  传入 `ObTenant`，之后在 4.x OMT 路径上不再被引用（死参数）；调它不会改变 worker 数。
  容量估算应改看租户 `min_cpu`/`max_cpu` 与 `cpu_quota_concurrency`。
- 大查询不是“按 `large_query_worker_percentage` 预留 worker”：4.x 通过 `lq_yield`
  把大查询迁入 `OBCG_LQ` cgroup 并按比例睡眠让出 worker，参数语义已变。

参数定义以 `src/share/parameter/ob_parameter_seed.ipp` 为准；跨版本先核对公式再引用。

## 5. 内存/线程排查清单

| 现象 | 先看 | 再看 |
| --- | --- | --- |
| 内存持续高位 | `GV$OB_MEMORY`/租户 hold、cache 命中率 | wash 频率、`memstore_limit_percentage` |
| 写入抖动/冻结频繁 | `freeze_trigger_percentage`、active memstore | 转储/合并耗时、IO 延迟 |
| SQL 慢但 CPU 不高 | 租户 CPU 配额、worker 数、排队 | 锁等待、IO 等待、大查询降级 |
| insert 上不去 | ApplySrv/ReplaySrv CPU、redo/IO | 跨 P 访存、L3、PALF 盘 |
| 只读 CPU 打满 | Backend/L3 Bound、微块解码 | 排序/比较、DAS 归并、内存分配 |
| 线程数异常 | `cpu_count`、`net_thread_count`、租户配额 | cgroup 是否限制、`_ob_max_thread_num` |
