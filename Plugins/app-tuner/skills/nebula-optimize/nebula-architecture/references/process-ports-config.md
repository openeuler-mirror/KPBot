# 进程协作、端口与配置参数

## 1. 五套 Thrift 服务

Nebula 使用 fbthrift（Facebook 的 Apache Thrift 分支）。IDL 在 `src/interface/`。

| IDL / 服务 | 调用方 → 服务端 | 默认端口 | 内容 | 频率 |
|-----------|----------------|----------|------|------|
| GraphService | client/console → graphd | 9669 | nGQL、查询结果 | 每次查询 |
| MetaService | graphd/storaged → metad | 9559 | 心跳、schema/分区拉取 | 约 10s |
| GraphStorageService | graphd → storaged | 9779 | 读写图数据 | 每次查询/写入 |
| StorageAdminService | metad → storaged | 9778（`port-1`） | addPart/compact/transLeader | 低频 |
| RaftexService | storaged → storaged | 9780（`port+1`） | 日志复制、投票、心跳 | 每次写入 + 周期心跳 |
| RaftexService（meta） | metad → metad | 9559 邻近端口 | 元数据 Raft | 元数据写入时 |

**端口约定**：一个 storaged 实例实际占用连续 3 个端口：`port-1`（admin）、`port`
（storage）、`port+1`（raft）。因此多实例规划必须留出 ≥3 的间隔，例如
9779/9782/9785/9788，而不是 9779/9780/9781/9782。

graphd 的 HTTP 端口为 `ws_http_port`（3.8.0 默认 19669；19670 是已弃用的 HTTP/2 端口，不要当默认），storaged 为 19779，metad 为 19559。多 graphd 实例用各自的 `${GRAPH_HTTP_PORT_N}`。
具体以配置文件为准。

## 2. Meta 数据分发：心跳 + 时间戳 + 拉取

不是 metad 主动推送，而是 graphd/storaged 定时拉取：

```
graphd/storaged MetaClient.bgThread
  → 每 heartbeat_interval_secs 发 HeartBeat
  → metad 返回最新元数据版本/变更
  → 若本地缓存版本落后，拉取增量并更新 localCache_
  → listener_ 通知 NebulaStore 分区变更
```

推论：

- 正常查询不需要 metad 参与，只读压测 metad CPU 接近 0。
- schema 变更后，各实例需要等待下一次心跳才完全生效；跨实例一致性是最终一致。
- graphd 实例越多，心跳与缓存副本越多，metad 的内存和刷新压力越大。
- `heartbeat_interval_secs` 调大减少 RPC，但会延长元数据变更生效时间。

## 3. Raft 通信

- 每个 Partition 是一个独立 Raft 组，但**所有 Part 共享同一个 RaftexService 端口**。
  RPC 请求里带 partId，由 `NebulaStore` 分发到对应 Part。
- 写路径：Leader `appendAsync` → 本地 WAL → 并行复制到 Follower → 多数派确认 → commit。
- 读路径默认走 Leader，不读 Follower 副本。
- 心跳/选举超时由 Raft 参数控制；网络抖动或磁盘长尾会触发重新选举，表现为写入抖动。
- metad 自己也有 Raft 组，用于元数据高可用。

## 4. graphd 配置参数

| 参数 | 默认 | 作用 | 调整建议 |
|------|------|------|----------|
| `--port` | 9669 | 客户端 GraphService 端口 | 多实例时递增 |
| `--num_netio_threads` | 0（=CPU 核数） | 网络收发/序列化线程 | 8–16 通常足够；过多增加调度开销 |
| `--num_worker_threads` | 0（=CPU 核数） | 查询执行线程 | 与实例数、瓶颈类型匹配；轻查询高 QPS 时过多会加剧队列争用 |
| `--num_accept_threads` | 1 | 连接接收线程 | 高连接数可调 2–4 |
| `--num_max_connections` | 0（不限） | 最大连接数 | 按并发与内存规划 |
| `--max_sessions_per_ip_per_user` | 300 | 每 IP 每用户 session 上限 | 压测需调大或设 0 |
| `--max_allowed_query_size` | 4MB | 单查询最大长度 | 大语句按需调大 |
| `--storage_client_timeout_ms` | 60000 | storaged RPC 超时 | 深遍历/大结果可调大，但会拉长故障暴露时间 |
| `--slow_query_threshold_us` | 200000 | 慢查询阈值 | 观测用 |
| `--enable_optimizer` | true | Cascades 优化器 | 简单 GO 可关；复杂 MATCH 建议开 |
| `--max_job_size` | 1 | 并行作业度 | >1 启用并行执行，需配合 `min_batch_size` |
| `--path_batch_size` | 10000 | 路径搜索批大小 | 影响路径查询内存 |
| `--system_memory_high_watermark_ratio` | 0.8 | 内存高水位 | 与 block cache 一起规划 |
| `--enable_authorize` | false | 鉴权 | 生产按需开启 |

## 5. storaged 配置参数

| 参数 | 默认 | 作用 | 调整建议 |
|------|------|------|----------|
| `--port` | 9779 | GraphStorageService 端口 | 多实例间隔 ≥3 |
| `--num_io_threads` | 16 | 网络 IO 线程 | 8–16 |
| `--num_worker_threads` | 32 | 查询执行线程 | 读密集可增；写密集看 WAL/compaction |
| `--reader_handlers` | 32 | reader 线程/处理器数 | 与读并发和 CPU 匹配 |
| `--query_concurrently` | true | 查询内并行执行 | 一般保持 true |
| `--data_path` | data/storage | 数据目录，逗号分隔多盘 | 每盘一个 RocksEngine |
| `--wal_ttl` | 14400 | WAL 保留秒数 | 只读压测可调小（如 60） |
| `--raft_heartbeat_interval_secs` | 30 | Raft 心跳 | 调小加快故障转移，代价是更多 RPC |
| `--raft_rpc_timeout_ms` | 500 | Raft RPC 超时 | 网络/磁盘慢时适当增大 |
| `--heartbeat_interval_secs` | 10 | Meta 心跳 | 与元数据生效时间权衡 |
| `--max_concurrent_subtasks` | 10 | 并发子任务 | 与读池容量匹配 |
| `--max_edge_returned_per_vertex` | INT_MAX | 每顶点最大返回边数 | 大度数顶点限流，防止单点爆量 |
| `--memory_tracker_limit_ratio` | 0.8 | 内存追踪比例 | 与 block cache 一起规划 |
| `--memory_purge_enabled` | true | jemalloc 后台清理 | 一般保持 |
| `--snapshot_part_rate_limit` | 10MB/s | 快照限速 | 避免快照打满磁盘 |
| `--rebuild_index_part_rate_limit` | 4MB/s | 重建索引限速 | 同上 |

## 6. metad 配置参数

| 参数 | 默认 | 作用 |
|------|------|------|
| `--port` | 9559 | MetaService 端口 |
| `--data_path` | data/meta | 元数据 RocksDB 目录 |
| `--default_parts_num` | 100 | 新建 Space 默认分区数 |
| `--default_replica_factor` | 1 | 新建 Space 默认副本数 |
| `--heartbeat_interval_secs` | 10 | 心跳间隔 |
| `--agent_heartbeat_interval_secs` | 60 | agent 心跳 |
| `--num_io_threads` | 16 | metad 网络 IO 线程（**分布式构建的消费入口**） |
| `--num_worker_threads` | 32 | metad worker 线程（**分布式构建的消费入口**） |
| `--meta_num_io_threads` | 16 | **仅 standalone 构建消费** |
| `--meta_num_worker_threads` | 32 | **仅 standalone 构建消费** |

> 线程参数按构建模式区分消费入口（v3.8.0 `src/daemons/MetaDaemonInit.cpp:70-76` 的
> `#ifndef BUILD_STANDALONE` 分支）：**分布式** nebula-metad 读
> `--num_io_threads`/`--num_worker_threads`，不消费 `meta_num_*`——后者 gflags 能解析
> 成功但 initKV 永不读取，配置无任何效果；**standalone** 才消费 `meta_num_*`。这是构建
> 模式的条件分支，不是"版本缺陷需源码修复"。
> standalone 模式下，若不显式设置 metad 的 Thrift 线程池，库可能按
> `hardware_concurrency()` 创建两份大线程池（如 384+384），造成大量空闲线程。
> 部署前核对实际线程数（`ps -o nlwp`）。

## 7. RocksDB 参数（storaged/standalone）

| 参数 | 典型值 | 作用 | 注意 |
|------|--------|------|------|
| `--rocksdb_block_cache` | 32768（MB） | 进程级共享 Block Cache | 超过 SST 实际大小后收益递减 |
| `--disable_page_cache` | true/false | 关闭 OS page cache，改用 O_DIRECT | 与 block cache 二选一，避免双重缓存 |
| `--rocksdb_compression` | lz4 | SST 压缩算法 | lz4 偏吞吐，zstd 偏空间 |
| `--rocksdb_batch_size` | 4096 | 写批大小 | 与写入模式匹配 |
| `--enable_rocksdb_prefix_filtering` | true | 前缀 Bloom Filter | 前缀扫描场景重要 |
| `--enable_rocksdb_whole_key_filtering` | false | 整键 Bloom Filter | 点查场景可开 |
| `--rocksdb_db_options` | `{}` | RocksDB DB 级选项 JSON | 可设 `max_background_jobs`、`max_subcompactions` |
| `--rocksdb_column_family_options` | JSON | CF 级选项 | `write_buffer_size`、`max_write_buffer_number`、`max_bytes_for_level_base` |
| `--rocksdb_block_based_table_options` | JSON | 表选项 | `block_size` 等 |
| `--engine_type` | rocksdb | 存储引擎 | 3.8.0 以 rocksdb 为主 |

## 8. 参数影响的判断顺序

不要从“默认值看起来小”直接推断要调大。先确定瓶颈：

```
QPS/延迟异常
  ├─ graphd CPU 高、futex 占比高 → 减少单实例 worker / 增加 graphd 实例
  ├─ graphd CPU 低、storaged CPU 高 → 看 RocksDB 扫描、GetNeighbors、schema 拷贝
  ├─ storaged CPU 低、写入慢 → WAL fsync、compaction、磁盘
  ├─ 内存高/ OOM → block cache、write buffer、水位、并发
  ├─ 长尾延迟 → compaction、快照、Raft 选举、NUMA/调度
  └─ 元数据变更慢 → heartbeat、metad 负载、graphd 实例数
```

参数只是杠杆之一；部署拓扑、数据模型（VID 类型、分区数）、查询写法往往影响更大。
优化顺序与实测收益见 `nebula-performance-optimization`。

## 9. 常见误解

| 误解 | 事实 |
|------|------|
| “metad 参与每次查询” | 查询读 MetaClient 本地缓存，metad 只做心跳/变更 |
| “storaged 的 3 个端口可以随便连号” | 每实例占 `port-1/port/port+1`，必须留 ≥3 间隔 |
| “Raft 每个 Part 一个端口” | 所有 Part 共享一个 RaftexService 端口，按 partId 分发 |
| “Follower 可以分担读” | 默认读 Leader，不读 Follower |
| “worker 越多 QPS 越高” | 轻查询高 QPS 时单队列 futex 争用会反噬 |
| “block cache 越大越好” | 超过工作集后 LRU 管理开销可能增加 |
| “standalone 所有腿都有 RPC 开销” | 存储腿无 RPC 开销：graphd→storaged 由 StorageClient 进程内直投 GraphStorageLocalServer；客户端→graphd、graphd/storaged→metad 仍走 loopback Thrift |
