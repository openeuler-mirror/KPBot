# 查询、写入与索引链路

## 1. 查询处理管线

```
Client RPC (GraphService.execute)
  → GraphSessionManager::findSession
  → QueryEngine::execute
    → 新建 QueryContext / QueryInstance
      → GQLParser::parse                生成 AST
      → Validator::validate             语义校验 + 生成 PlanNode 树
      → Optimizer::findBestPlan         Cascades 风格探索（可关闭）
      → AsyncMsgNotifyBasedScheduler    Executor DAG 调度
      → fillRespData                    结果组装与序列化
```

要点：

- 解析器是 flex/bison；INSERT 场景下词法+语法分析可占 graphd CPU 的可观比例。
- 校验阶段有约 30 个 Validator；复杂语句的校验/类型推导也会进入热点。
- 优化器可被 `--enable_optimizer=false` 关闭；简单 GO 语句常无明显收益，反而省掉调度开销。
- 执行器是异步 DAG：节点之间用 `folly::Future/Promise` 连接，不是同步递归。

## 2. 分区路由

graphd 不“广播到所有 storaged”，而是按 VID 算分区后定向发请求：

```
StorageClient::clusterIdsToHosts:
  partId = hash(VID) % numParts + 1   // 分区号从 1 起；8 字节 VID 直接取整数值，其他长度走 MurmurHash2
  leader = MetaClient.localCache.partHosts[partId]
  按 HostAddr 分组
  并行发送 GraphStorageService RPC 到各 storaged
```

| 查询类型 | 路由方式 |
|----------|----------|
| FETCH / INSERT / DELETE（已知 VID） | 精确路由到该 VID 的分区 leader |
| GO 1 STEP | 按起点 VID 路由，一次 RPC |
| GO N STEP | 每跳按当前顶点集合重新路由，N 轮串行；每轮内顶点按目标主机分组并行发多个 RPC |
| LOOKUP（不知道 VID） | 广播到所有分区 leader，并行扫描索引 |
| MATCH 变长路径 | 由执行器按跳展开，通常多轮 RPC |

推论：

- 单跳一轮内的顶点按命中的 storaged 主机分组、并行发出多个 RPC（同跳顶点命中两台
  storaged 时即产生两个并行请求）；多跳查询是轮次串行、每轮 RPC 数随扇出增长。
  估算多跳请求量应按"轮数 × 每轮 RPC 数"计，不能只按跳数算 1 次 RPC。
- graphd 的 worker 在两次 RPC 之间通常处于 Future 等待，CPU 不一定饱和。
- 增加 graphd 会分摊队列争用，但也会放大对 storaged 的并发 RPC 压力。

## 3. 异步 DAG 与 folly Future

### 3.1 为什么用 Future

worker 发出 storaged RPC 后不阻塞等待，而是注册回调后继续处理别的任务。storaged 响应到达
graph-netio 线程后，`Promise::setValue` 触发回调，回调被 `executor_->add()` 投递回 worker 池。

```
Worker-A: 发 RPC → f.via(runner).thenValue(cb) → 注册回调 → 返回
graph-netio: recv 响应 → 反序列化 → setValue → doCallback → add(cb)
Worker-B: 取到 cb → setContext → 执行后续 DAG 节点
```

关键：Worker-A 与 Worker-B 可以不是同一线程；查询状态都在堆上（QueryContext/Executor/Core），
`RequestContext` 保存 thread_local 快照，回调执行前恢复。

### 3.2 开销来源

- 每个 RPC 会构造多条 Future 链（`deferValue` / `thenTry` / `thenError` / `thenValue`），
  每条链涉及 `CoreBase` 引用计数、`RequestContext::setContext`、`shared_ptr` 原子操作。
- 轻查询、高 QPS 时这些开销被队列开销掩盖；队列争用下降后会暴露出来。
- 实测中 4 graphd 相比 2 graphd，graphd 的 folly Future/上下文占比从约 2.5% 升到约 17.6%，
  因为 futex 瓶颈被拆散后，回调链成为新的主要开销之一。

### 3.3 调度器

`AsyncMsgNotifyBasedScheduler::doSchedule` 按 DAG 依赖创建 Promise/Future 对，依赖满足后
投递下一个 Executor。DAG 并行度受 `--max_job_size`、`--min_batch_size` 等参数影响。

## 4. 一次 GO 1 STEP 的完整时间线

```
① k6 → graphd
   graph-netio: recv → 反序列化 → ThreadManager::add

② Worker-A
   waitOnTask → doSchedule → getNeighbors → via(evb)
   序列化请求 → sendmsg → 注册回调 → 返回等待

③ storaged
   io 线程 recv → 反序列化 → worker/reader 执行
   GetNeighborsProcessor → RocksDB prefix scan → 组装结果
   序列化响应 → sendmsg

④ graphd graph-netio
   recv → 反序列化 → doCallback → add 回调到 worker 池

⑤ Worker-B
   处理响应 → handleJob → 结果组装 → 序列化 → sendmsg 给客户端
```

四次序列化/反序列化都发生在 graph-netio/io 线程：

1. 客户端 → graphd（服务端接收）
2. graphd → storaged（客户端发送）
3. storaged → graphd（客户端接收）
4. graphd → 客户端（服务端发送）

这是 RPC 架构的固有成本，也是“进程内调用”与“跨进程 RPC”差异的来源。

## 5. 写入链路

以 `INSERT VERTEX player(name, age) VALUES "Kobe":("Kobe", 41)` 为例：

```
graphd:
  解析 → 校验 → InsertVerticesExecutor
  → StorageClient::addVertices
    → 按 VID 哈希分区
    → 按 leader 分组，并行发 AddVerticesRequest

storaged:
  1. 编码 KV
     Tag Key = 0x01|partId|vid|tagId
     Value   = RowWriterV2 编码的属性
     有索引时追加 Index Key
  2. 放入 WriteBatch
  3. Raft:
     appendAsync → 写 WAL → 复制到 Follower → 多数派确认 → commit
  4. commit 到 RocksDB:
     Part::commitLogs → RocksEngine::commitBatchWrite → rocksdb::DB::Write
```

写入热点通常不在“写 RocksDB”本身，而在：

- graphd 的 nGQL 解析与 KV 编码；
- storaged 的 Raft 日志编码、WAL fsync（内核态 `fdatasync`）；
- RocksDB WriteThread 等待、MemTable/SST 构建、compaction（LZ4 压缩、BlockBuilder）；
- 索引维护带来的额外 Key 写入。

## 6. 索引查找转换

以 `LOOKUP ON player WHERE player.age > 20 YIELD player.name` 为例：

1. graphd 优化器选择可用索引（如 `player_age_index`），构造 IndexQueryContext。
2. 发给所有分区（LOOKUP 不知道 VID）。
3. storaged 取索引元数据，构建 IndexNode 执行树。
4. 把 WHERE 条件转换为 RocksDB Key 范围：
   - 等值：`indexPrefix + encode(value)`，做 prefix scan；
   - 范围：`[encode(lower), encode(upper))`，做 range scan。
5. 扫描 Index Key 得到 VID，再回表读 Tag Key 取属性。

编码保证字典序 = 大小序：INT64 翻转符号位后大端；Double 正数翻符号位、负数全取反；
String 截断补 `\0`；Rank 与 INT64 同法。详见 `storage-key-encoding.md`。

## 7. Raft 与读一致性

- 写：Leader appendAsync → WAL → 复制 → 多数派 commit。
- Raft 心跳按服务区分：storaged 部署默认 30s（`etc/nebula-storaged.conf.default` 显式
  `--raft_heartbeat_interval_secs=30`；flag 源码默认是 5s）；metad 的 conf 未设置该
  flag、实际走源码默认 5s。调小加快故障转移时，storaged 从 30s 调起、metad 从 5s 调起。
- 读默认走 Leader（Raft 读不复制日志），因此读延迟主要取决于 Leader 本地 RocksDB。
- Follower 只参与复制与选举，不直接服务普通读请求。
- metad 自身也用 Raft 做高可用；metad 的 leader 变更会影响 schema/分区变更，但正常查询
  走 MetaClient 缓存，影响有限。

## 8. 链路排查清单

| 现象 | 先看 |
|------|------|
| graphd CPU 高但 QPS 不涨 | Thrift 队列 futex、Future 回调链、内存分配 |
| graphd CPU 低、延迟高 | worker 在等 storaged RPC；看 storaged CPU/RocksDB |
| storaged CPU 高 | RocksDB 迭代/比较、GetNeighbors 组装、schema 拷贝 |
| storaged CPU 低、写入慢 | WAL fsync、compaction、磁盘 I/O、Raft 复制 |
| metad CPU 高 | 频繁 schema/分区变更、心跳异常、大量 graphd 实例刷新 |
| 单跳快、多跳慢 | 轮次串行等待；看轮数与每轮按主机分组并行发出的 RPC 数 |
| LOOKUP 慢 | 索引是否命中、是否广播到所有分区、索引前缀是否足够长 |

采集命令与场景化热点见 `nebula-hotspot-analysis`。
