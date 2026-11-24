# NebulaGraph 3.8.0 总览与源码地图

## 1. 三服务架构

```
                     ┌───────────────────────────────┐
 Client / console ──▶│ graphd  (GraphService, 9669)  │
                     │ 解析 → 校验 → 优化 → 调度执行  │
                     └───────┬───────────────┬───────┘
                             │ MetaService   │ GraphStorageService
                             ▼               ▼
                     ┌───────────────┐  ┌──────────────────────────┐
                     │ metad         │  │ storaged                 │
                     │ Schema/分区/  │  │ RocksDB + Raft + 索引    │
                     │ 用户/作业/心跳│  │ 每分区一个 Raft 组       │
                     └───────┬───────┘  └────────────┬─────────────┘
                             │ Raft                  │ RaftexService
                             ▼                       ▼
                     其他 metad 副本          其他 storaged 副本
```

| 服务 | 职责 | 是否有状态 | 横向扩展 |
|------|------|-----------|----------|
| graphd | nGQL/Cypher 解析、语义校验、计划生成、Cascades 优化、DAG 调度、结果组装 | 无状态 | 直接加实例 |
| metad | Space、Tag/Edge schema、分区分配、用户/RBAC、Session、作业、心跳、负载均衡 | 有状态（Raft） | 多副本 Raft |
| storaged | 顶点/边/索引持久化、GetNeighbors/GetProp/Scan、Raft 复制、TOSS 事务、监听器 | 有状态 | 加节点/分区副本 |

## 2. 核心设计

1. **存储计算分离**：graphd 无状态；storaged 持有数据；metad 管元数据。
2. **分区即 Raft 组**：每个 Partition 独立复制组，互不阻塞。
3. **出边邻接表**：同一源顶点的出边在 RocksDB 中连续存放，前缀扫描即单跳遍历。
4. **Key 编码驱动查询**：把 `(space, part, type, src, edgeType, rank, dst)` 编进 Key，
   让字节字典序等于语义顺序（详见 `storage-key-encoding.md`）。
5. **双向边存储**：每条逻辑边物理上存正向出边和反向入边；出边在源分区，入边在目标分区，
   反向边 edgeType 取负、src/dst 互换。跨分区写入需要 TOSS 或应用侧保证一致性。
6. **shared-nothing**：每个 storaged 独立 RocksDB 与磁盘，不共享存储。

## 3. 数据模型与分片

| 概念 | 说明 |
|------|------|
| Space | 图空间，类似数据库，隔离 schema 与数据 |
| Tag | 顶点类型；一个顶点可有多个 Tag，各自带属性 schema |
| EdgeType | 有向边类型，带属性 schema |
| Vertex | 由 VID 唯一标识，VID 为 INT64 或 FIXED_STRING(N) |
| Edge | `(src, edgeType, rank, dst)` 四元组唯一；rank 区分同源同类型同目标的多条边 |
| Partition | 数据分片；`partId = hash(VID) % numParts + 1`（分区号从 1 起）；8 字节 VID 直接取整数值，其他长度走 MurmurHash2 |

分片要点：

- 同一顶点的 Tag 与出边落在同一分区，图遍历只访问一个分区。
- `numParts` 在建 Space 时确定，事后修改成本高；分区过多会增加元数据与导入开销。
- 实测 SF100（2.8 亿顶点 + 17.8 亿边）上 96 分区与 192 分区全场景 ±3%，分区并行度通常
  不是瓶颈；192 分区导入耗时约 4 倍，默认优先 96。
- `replica_factor` 与 `data_path` 相互独立：前者是 Raft 副本数，后者是单机多盘目录数。

## 4. 进程模型

### 4.1 standalone（单进程三线程）

- 编译选项：`-DENABLE_STANDALONE_VERSION=ON`，产物 `nebula-standalone`。
- `StandAloneDaemon.cpp` 创建 `metaThread`、`graphThread`、`storageThread`。
- 两腿仍走 Thrift RPC：客户端→graphd、graphd/storaged→metad（meta_port）。graphd→storaged 这条
  最高频的存储腿在 standalone 构建下不走 Thrift：`StorageClient` 的 `BUILD_STANDALONE` 分支
  （`src/clients/storage/StorageClient.h`）直接投递到进程内 `GraphStorageLocalServer`
  （`src/storage/GraphStorageLocalServer.cpp`，经 ThreadManager 派发，无端口与序列化）。
  本地服务刻意模拟了异步 RPC 语义，因此行为与分布式一致。
- 优点：部署简单、无跨机网络；缺点：一个进程内的 worker 队列更容易出现 futex 争用，
  且不能通过多 graphd 实例分摊队列。

### 4.2 分布式（独立进程）

- 编译选项：`-DENABLE_STANDALONE_VERSION=OFF`，产物 `nebula-metad / nebula-graphd /
  nebula-storaged`。
- 单机也可部署多 graphd + 多 storaged（常称 1P 分布式），用于在单机上验证多进程架构。
- 多 graphd 分摊 Thrift 队列争用、提升读吞吐；多 storaged 分摊 RocksDB 与 schema 处理，
  但会增加 RPC 并发与内存占用。

### 4.3 线程池

| 进程 | 线程池 | 参数 | 典型实测值 |
|------|--------|------|-----------|
| graphd | netio（网络收发/序列化） | `--num_netio_threads` | 8 |
| graphd | worker（查询执行） | `--num_worker_threads` | 20 / 48（按实例数调整） |
| graphd | accept | `--num_accept_threads` | 1 |
| storaged | io | `--num_io_threads` | 8–16 |
| storaged | worker | `--num_worker_threads` | 8–48 |
| storaged | reader handlers | `--reader_handlers` | 8–32 |
| storaged | Raft bg workers | 内部 | 4 |
| metad | meta io / worker | 分布式：`--num_io_threads` / `--num_worker_threads`；standalone：`--meta_num_io_threads` / `--meta_num_worker_threads`（仅 standalone 构建消费，见 process-ports-config.md §6） | 空闲时最小化 |
| kvstore | 后台 worker | `--num_workers` | 4 |

线程数没有通用最优值：先看瓶颈是队列争用、RPC 等待还是 RocksDB 扫描，再调。
worker 越多，单队列 futex 争用越强；实例数增加时单实例 worker 应相应减少。

## 5. MetaClient 缓存与心跳

- graphd/storaged 进程内都有一个 MetaClient 对象（不是独立进程）。
- `localCache_` 保存 SpaceInfoCache：space id/name、schema、partHosts、索引、leader 信息。
- 查询时读本地缓存，正常路径 0 次 metad RPC；后台线程约每 10s 心跳并拉取变更。
- 因此只读压测中 metad CPU ≈ 0 是正常现象，不要据此判断 metad 无用。
- 多 graphd 实例意味着多份缓存，实例数越多，元数据内存与刷新 RPC 越多。

## 6. 内存与锁（定位热点时需要）

### 内存

- 编译期 `ENABLE_MEMORY_TRACKER` 会在每次 new/delete 记账，高频小对象场景有可测开销；
  压测/生产可按需关闭。
- 水位控制：`system_memory_high_watermark_ratio`（graphd）、
  `memory_tracker_limit_ratio`（storaged）。
- RocksDB 内存：`rocksdb_block_cache`（进程级共享 LRU）、`write_buffer_size`、
  `max_write_buffer_number`、`max_bytes_for_level_base`。
- Block Cache 是所有 Space 共享的 `static` 对象；打开多个 Space 不等于多份 cache。

### 锁与事务

- 三个锁域：schema/元数据、Raft 内部、存储引擎/RocksDB。
- TOSS 是跨分区事务框架（Chain + Prime/Double-Prime 两阶段 + Resume），代码存在但运行时
  默认禁用；不要把它当成默认可用的事务能力。
- 写入一致性主要依赖「按 VID 路由 + 单分区 Raft」；跨分区边写入的原子性需要额外设计。

## 7. 源码地图（按问题找文件）

| 问题 | 入口 |
|------|------|
| 查询总入口/会话 | `src/graph/service/QueryInstance.cpp`、`QueryEngine`、`session/` |
| 解析 | `src/parser/`、`src/graph/validator/` |
| 计划与优化 | `src/graph/planner/`、`src/graph/optimizer/` |
| 调度与执行器 | `src/graph/scheduler/`、`src/graph/executor/`、`src/graph/context/` |
| 分区路由/RPC | `src/clients/storage/StorageClient.cpp`、`src/clients/meta/MetaClient.cpp` |
| Key 构建 | `src/common/utils/NebulaKeyUtils.cpp`、`IndexKeyUtils.*` |
| 行编解码 | `src/codec/RowWriterV2.cpp`、`RowReaderV2.cpp` |
| 读处理器 | `src/storage/query/GetNeighborsProcessor.*`、`GetPropProcessor.*` |
| 写处理器 | `src/storage/mutate/AddVerticesProcessor.*`、`AddEdgesProcessor.*` |
| 存储执行算子 | `src/storage/exec/`（TagNode/EdgeNode/HashJoinNode/IndexScanNode） |
| Raft/WAL | `src/kvstore/raftex/`、`src/kvstore/wal/` |
| RocksDB 封装 | `src/kvstore/RocksEngine.*`、`RocksEngineConfig.*` |
| 分区分发 | `src/kvstore/NebulaStore.*`、`src/kvstore/Part.*` |
| Thrift IDL | `src/interface/`（GraphService/MetaService/GraphStorageService/StorageAdminService） |

## 8. 规模与容量经验

以下为鲲鹏 950、384 逻辑核、749GB、双 NVMe、LDBC SF100 的实测观察，只用于建立假设：

- graphd 是轻查询（Go1）的第一瓶颈；增加 graphd 实例数比增加单实例 worker 更有效。
- Go2/Go3 的瓶颈在 storaged 的 RocksDB 扫描与结果组装；增加 storaged 数量或读池容量有效。
- FindShortestPath 的瓶颈在 graphd 的 BFS 容器与 Value 字符串操作。
- Insert 的瓶颈在 nGQL 解析、Raft WAL fsync 与 RocksDB compaction，CPU 往往不饱和。
- 全浮动部署（进程共享核池、由调度器动态分配）在混合负载下利用率最高；固定绑核在单一
  点查型负载下可能更快，但会失去跨场景弹性。

这些结论对应的采集方法与数据见 `nebula-hotspot-analysis`，优化杠杆与实测收益见
`nebula-performance-optimization`。
