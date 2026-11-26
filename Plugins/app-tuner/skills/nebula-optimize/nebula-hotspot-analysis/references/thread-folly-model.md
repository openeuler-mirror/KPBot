# 线程模型与 folly Future

理解线程边界，才能把火焰图里的函数正确归因到“计算、等待、序列化、锁”。

## 1. 线程池

### graphd

| 线程名（示例） | 角色 | 关键工作 |
|---------------|------|---------|
| `graph-netio` | 网络 IO | 收客户端请求/发响应、收 storaged 响应、序列化/反序列化、入队 |
| `executor-pri3-*` | worker | 取任务、解析后的执行、DAG 调度、结果组装 |
| `graph-memory-mo` | 内存监控 | 水位检查 |
| `hazptr-tpe` | hazard pointer | 内存回收 |
| accept 线程 | 连接接收 | 建连 |

### storaged

| 线程名（示例） | 角色 | 关键工作 |
|---------------|------|---------|
| `executor-pri3-N` | thrift worker（**与 graphd 同名前缀，跨进程须按 PID 区分**） | 写/管理等非读 processor 的执行 |
| `reader-pool-pri3-N` | 读执行（默认 `reader_handlers_type=cpu`；配 io 时为 `reader-pool-N`） | GetNeighbors/GetProp/Scan、RocksDB 迭代、结果组装 |
| `IOThreadPool<N>` | 网络 IO（folly 默认命名，大写） | 收 graphd 请求、发响应、序列化/反序列化 |
| Raft bg workers | Raft | 日志复制、心跳、选举 |
| `nebula-bgworke*` | KV 后台（comm 15 字符截断） | flush/compaction 协调 |
| `rocksdb:*` | RocksDB 后台 | compaction/flush |

注意：storaged 不存在名为 `storage-worker` 或小写 `io` 的线程——通用 thrift worker 与
graphd 同用 `executor-pri3-*` 前缀（StorageServer.cpp setNamePrefix("executor")），
IO 线程是 folly `IOThreadPool<N>`。

### metad

正常查询不经过 metad；心跳、schema/分区变更时才活跃。压测中 metad CPU≈0 正常。

## 2. Thrift ThreadManager 与队列

graphd/storaged 的 fbthrift 服务使用 `PriorityThreadManager` + `PriorityUnboundedQueueSet`：

```
graph-netio: recv → 反序列化 → ThreadManager::add(task) → 入队 + futex wake
worker:     waitOnTask → futex wait/唤醒 → try_dequeue → 执行
```

关键函数：

| 函数 | 含义 |
|------|------|
| `ThreadManager::Impl::add` | IO 线程入队并唤醒 worker |
| `ThreadManager::Impl::waitOnTask` | worker 等新任务（futex wait） |
| `PriorityUnboundedQueueSet::try_dequeue` | worker 尝试取任务（futex/锁） |

为什么轻查询会卡在这里：

- Go1 每次查询 ~1ms，QPS 十几万，每秒几十万次入队/出队。
- 所有 worker 共享一个队列；worker 数 N，争用近似 O(N²)。
- 48 worker 争一个队列时，futex 可占 graphd CPU 的 50%+；减少到 16 worker 或拆成多个
  graphd 实例，争用显著下降。

推论：

- “加 worker”不一定提升 QPS；轻查询高 QPS 时可能更差。
- 增加 graphd 实例（每实例较少 worker）通常比单实例加线程有效。
- 根因在第三方 Thrift 队列，改造成本高；优先用部署手段缓解。

## 3. folly Future/Promise

### 3.1 结构

```
Promise (生产端)          Core (堆, shared_ptr)          Future (消费端)
  setValue(result) ─────▶ result_ / callback_ / executor_ / requestContext_
                                                        ◀──── getFuture()
                                                        ◀──── via(runner)
                                                        ◀──── thenValue(cb)
```

- `Core` 在堆上，引用计数；不属于任何线程。
- worker 注册回调后返回，不阻塞；响应到达时由 netio 线程 `setValue` 触发 `doCallback`。
- `via(executor)` 指定回调投递到 worker 池；否则可能在当前线程内联执行。
- `RequestContext` 保存 thread_local 快照，回调执行前恢复。

### 3.2 典型用法

```cpp
Promise<Response> p;
Future<Response> f = p.getFuture();

f.via(runner)
 .thenValue([](Response resp) { process(resp); });

storageClient->sendAsync(p);   // 把 Promise 交给 netio
// worker 返回，去处理别的任务
```

多个 storaged 并发时用 `collectAll` 等全部返回；任一失败用 `collect` 提前结束。

### 3.3 开销

每条 Future 链涉及：

- `CoreBase` 引用计数（原子操作）；
- `RequestContext::setContext`；
- `shared_ptr`/`KeepAlive`；
- 回调对象构造/析构。

轻查询高 QPS 时这些开销被队列掩盖；队列争用下降后（如 4 graphd），Future 回调链可占
graphd CPU 的 15–20%。这不是“业务计算”，而是异步框架成本。

## 4. 四次序列化/反序列化

一次 GO 1 STEP：

```
① k6 → graphd      graph-netio: recv → 反序列化 → ThreadManager::add
② graphd → storaged graph-netio: 序列化请求 → sendmsg
③ storaged → graphd graph-netio: recv → 反序列化 → doCallback → add
④ graphd → k6       graph-netio: 序列化响应 → sendmsg
```

所以 graph-netio 的 CPU 不只是 `sendmsg/recv`，还包括 Thrift 编解码。Go2/Go3 结果集大时，
序列化/反序列化占比上升；这也是“返回列越少越快”的原因之一。

## 5. DAG 调度

- 执行计划是 DAG，每个节点是 Executor。
- `AsyncMsgNotifyBasedScheduler::doSchedule` 为依赖创建 Promise/Future 对。
- 依赖满足后投递下一个 Executor；并行度受 `max_job_size`/`min_batch_size` 等影响。
- worker-A 与 worker-B 可能不是同一线程；查询状态都在堆上，通过指针访问。

热点含义：

- 调度器本身占比通常不高，但 Future 链、上下文切换会累积。
- 复杂 DAG（多跳、聚合、路径）会增加调度与回调次数。

## 6. storaged 读路径

```
io 线程 recv → 反序列化 → ThreadManager::add
worker/reader:
  GetNeighborsProcessor::process
    → SchemaManager::getAllVerTagSchema()/getAllVerEdgeSchema()  （值拷贝！）
    → 构建执行树（TagNode/EdgeNode/HashJoinNode/...）
    → RocksDB prefix scan
    → RowReaderV2 解码
    → 组装 DataSet/Value
  → 序列化响应 → sendmsg
```

热点对应：

- `std::_Hashtable` 构造/析构：schema 表值拷贝（每查询一次）。
- `RocksDB IndexBlockIter/DataBlockIter/MergingIterator`：前缀/范围扫描。
- `GetNeighborsNode::iterateEdges`、`HashJoinNode::next`：结果组装。
- `Value::setS`/`Value` 构造：属性解码与结果构造。
- `malloc/sdallocx/tcache`：每行/每迭代分配。

## 7. 写路径

```
graphd: 解析 → 校验 → InsertExecutor::insertVertices → 编码 KV → RPC
storaged: WriteBatch → Raft appendAsync → WAL fsync → 复制 → commit
          → RocksDB Write → MemTable → flush/compaction
```

热点对应：

- graphd：`GraphScanner::yylex`、`GraphParser::parse`、Value/RowWriter。
- storaged：`WriteThread::AwaitState`、`LZ4_compress_fast_continue`、`BlockBuilder`、
  内核 `fdatasync/fsync`、Raft 日志编码。

## 8. 常见疑问

**Q：`waitOnTask` 是在等空队列还是锁争用？**
两者都有：队列空时 futex wait；有任务时唤醒/竞争。高 QPS 下主要是争用与唤醒开销。

**Q：worker 在 `waitOnTask`，为什么还有 `doSchedule`？**
worker 取到任务后执行 `doSchedule`；采样会把同一线程的不同阶段都计入，需看调用栈。

**Q：`CoreBase::setResult` 为什么还要放回 worker 池？**
`via(executor)` 要求回调不在 netio 线程执行，避免阻塞网络线程，同时恢复
`RequestContext`。

**Q：`Value::clearSlow` 什么时候触发？**
Value 持有字符串/复杂类型析构时；FSP/多跳结果多时占比高。

**Q：Storage 回调由哪个 worker 处理？**
由 `via(runner)` 指定的 worker 池中的任意线程，不保证与发起线程相同。

**Q：`StatsManager::addValue` 是什么？**
HTTP `/stats` 的指标累加，高频路径有少量开销。

## 9. 归因速查

| 火焰图热点 | 所属线程 | 机制 | 优先优化 |
|-----------|---------|------|---------|
| `try_dequeue`/`waitOnTask`/`ThreadManager::add` | graphd worker/netio | Thrift 队列 futex | 增加实例/减少 worker |
| `sendmsg`/`recv`/`epoll` | netio/io | 网络与序列化 | 减少 RPC/结果集、批量 |
| `malloc`/`sdallocx`/`tcache` | worker/reader | 内存分配 | 对象池、减少拷贝 |
| `Value::setS`/`clearSlow` | graphd/storaged | 字符串/值构造 | SSO、减少拷贝 |
| `_Hashtable` 构造/析构 | storaged | schema 拷贝 | 缓存/shared_ptr |
| RocksDB iter/memcmp | storaged | 前缀扫描/比较 | Key/索引/缓存 |
| `GraphScanner::yylex`/`GraphParser::parse` | graphd | nGQL 解析 | 预编译/减少语句复杂度 |
| `WriteThread::AwaitState`/`LZ4`/`fsync` | storaged | 写路径/compaction | WAL/compaction/磁盘 |
| `CoreBase`/`RequestContext` | worker | Future 回调 | 减少回调链（框架级） |
