# 场景化热点与瓶颈迁移

> 数据来自鲲鹏 950（96C/192T，4 NUMA，749GB，双 NVMe）、LDBC SF10/SF100、
> Nebula 3.8.0 的实测。百分比是 perf 采样/调用链聚合的近似值，随数据、部署、版本变化；
> 用来建立假设，不作为通用规格。x86 对照见 §7。

## 1. 五场景总览

| 场景 | QPS 量级（SF10，2g+2s） | 延迟特征 | 瓶颈位置 | 核心机制 |
|------|------------------------|---------|---------|---------|
| Go1Step | ~18–19 万 | p50 ~1.2ms | graphd | Thrift 队列 futex + RPC/序列化 |
| Go2Step | ~1.4 万 | p50 ~27ms | storaged | RocksDB 扫描 + GetNeighbors 组装 + 内存 |
| Go3Step | ~500 | p50 ~43ms，p99 ~0.9s | storaged | 大扇出扫描 + 结果组装 + 队列积压 |
| FindShortestPath | ~9 千 | p50 ~16ms | graphd | BFS 容器 + Value 字符串 |
| InsertPerson | ~8.5 千 batch/s | p50 ~43ms | I/O | nGQL 解析 + WAL fsync + compaction |

> 不同部署（2g/2s、4g/4s、8g/4s、standalone）绝对值差异很大，比例与迁移规律更可复用。

## 2. Go1Step（轻读）

### graphd

| 分类 | 占比 | 说明 |
|------|------|------|
| Thrift 队列 futex | **~59%** | `try_dequeue` + `waitOnTask` + `ThreadManager::add` + 内核 futex |
| 内核态网络 | ~15% | sendmsg/recv/epoll |
| 内存分配/释放 | ~11% | jemalloc sdallocx/malloc/tcache |
| 锁竞争 | ~9% | pthread_mutex/folly SharedMutex |
| folly Future/上下文 | ~4% | RequestContext/keepAlive |
| 查询执行 | ~1% | Scheduler/MetaClient 缓存 |

关键：查询太轻（~1ms）、QPS 太高，worker 大量时间在队列 futex 上，而不是业务计算。
单实例 worker 越多，单队列争用越强（48 worker 时尤其明显）。

### storaged

| 分类 | 占比 | 说明 |
|------|------|------|
| 内核态网络 | ~20% | 响应发送/请求接收 |
| RocksDB 查询 | ~12% | IndexBlockIter/DataBlockIter/memcmp |
| GetNeighbors 处理 | ~12% | SingleEdgeIterator/Value 构造 |
| 内存分配/释放 | ~11% | malloc/sdallocx/tcache |
| **schema 哈希表拷贝** | **~8%** | 每查询深拷贝 schema 表后析构 |
| futex | ~10% | worker 队列 |
| 锁竞争 | ~4% | pthread_mutex |
| folly Future | ~3% | 回调 |

### 优化方向

- **部署**：增加 graphd 实例、降低单实例 worker（实测 4 graphd 相比 2 graphd QPS +44%）。
- **源码**：减少 schema 拷贝（返回 shared_ptr）、对象池、减少 Value 分配。
- **框架**：per-thread 队列/替换 ThreadManager（收益大但风险高，属第三方层）。

## 3. Go2Step（中读，多跳）

### graphd

| 分类 | 占比 | 说明 |
|------|------|------|
| 内存分配/释放 | **~35%** | 每次 RPC 结果产生大量 Value 对象 |
| futex | ~15% | 仍存在但不再是 #1 |
| 锁竞争 | ~12% | Thrift 内部锁 |
| folly Future | ~10% | 等 RPC 回调 |
| Value 操作 | ~8% | 拷贝构造/setS/clearSlow |
| 内核网络 | ~8% | sendmsg/recv |
| Thrift 序列化 | ~5% | CompactProtocol |
| 查询执行 | ~3% | Traverse/Scheduler |

### storaged

| 分类 | 占比 | 说明 |
|------|------|------|
| **RocksDB 查询** | **~25%** | DBIter/MergingIterator/IndexBlockIter/DataBlockIter/memcmp |
| **内存分配/释放** | **~22%** | 每行/每迭代分配 Value/行对象 |
| **GetNeighbors 处理** | **~15%** | iterateEdges/HashJoinNode/RowReader/setS |
| 内核态网络 | ~12% | 收发 |
| futex | ~6% | 队列 |
| 锁竞争 | ~5% | pthread_mutex |
| folly Future | ~5% | 回调 |

关键：Go2 每查询约 11 次 RPC（1 跳邻居数），storaged 承受约 11 倍 Go1 的请求量；
graphd worker 大部分时间在等 RPC，所以 CPU 利用率低（约 26%），storaged 成为瓶颈（约 56%）。

## 4. Go3Step（重读，大扇出）

### graphd

| 分类 | 占比 | 说明 |
|------|------|------|
| 内存分配/释放 | **~42%** | 三跳中间结果（N²×M 个 Value） |
| folly Future | **~20%** | 大量串行 RPC 等待 |
| Value 操作 | ~10% | 拷贝/setS/clearSlow |
| futex | ~8% | worker 多在等 RPC，队列争用下降 |
| 内核网络 | ~7% | 收发 |
| 锁竞争 | ~5% | pthread_mutex |

### storaged

| 分类 | 占比 | 说明 |
|------|------|------|
| **RocksDB 查询** | **~30%** | 更多 SST index/data block 扫描 |
| **内存分配/释放** | **~25%** | 大量结果对象 |
| **GetNeighbors 处理** | **~18%** | iterateEdges/HashJoin/RowReader/setS |
| 内核态 | ~12% | 收发 |
| 锁竞争 | ~5% | pthread_mutex |

关键：瓶颈完全在 storaged；三跳导致指数级 RPC 与扫描，graphd 42% 在内存分配。
高并发下还要关注 storaged 队列积压与成功率（深查询需要足够读池容量）。

## 5. FindShortestPath（BFS）

### graphd

| 分类 | 占比 | 说明 |
|------|------|------|
| **Value 字符串操作** | **~25%** | clearSlow + setS，VID 字符串创建/释放 |
| **哈希表（BFS visited/路径）** | **~15%** | `_M_insert_multi_node`、哈希表析构 |
| 内存分配/释放 | ~20% | sdallocx/malloc/tcache |
| folly Future | ~15% | 多跳 BFS RPC |
| futex | ~8% | 队列 |
| 内核网络 | ~7% | 收发 |
| BFS 算法 | ~5% | BFSShortestPathExecutor（单对最短路）/路径构建 |
| 锁竞争 | ~3% | pthread_mutex |

### storaged

| 分类 | 占比 | 说明 |
|------|------|------|
| **内存分配/释放** | **~25%** | malloc/arena/sdallocx |
| **RocksDB 查询** | **~22%** | DBIter/Merging/DataBlock/memcmp |
| **GetNeighbors 处理** | **~18%** | iterateEdges/HashJoin/readEdgeProp/SingleEdgeIterator |
| 内核态 | ~10% | 收发 |
| folly Future | ~5% | 回调 |
| 锁竞争 | ~3% | pthread_mutex |

关键：瓶颈在 graphd 的 BFS 数据结构与 Value 字符串，而不是单纯 RocksDB。
把 `unordered_multimap` 换成 flat_map/减少 Edge 拷贝/删除去重容器，可显著改善（实测 FSP +15%）。

## 6. InsertPerson（写）

### graphd

| 分类 | 占比 | 说明 |
|------|------|------|
| 内存分配/释放 | **~35%** | 解析 AST + KV 编码 |
| **nGQL 解析** | **~22%** | flex/bison 词法+语法 |
| Value 操作 | ~10% | 构造/析构、RowWriter |
| folly Future | ~8% | RPC 回调 |
| futex | ~5% | 低并发，不饱和 |
| Thrift 序列化 | ~5% | KV 编码后发送 |
| 内核态 | ~5% | 收发 |
| 锁竞争 | ~3% | pthread_mutex |

### storaged

| 分类 | 占比 | 说明 |
|------|------|------|
| **RocksDB compaction** | **~13%** | CompactionJob + LZ4 压缩 + BlockBuilder |
| **RocksDB 写路径** | **~15%** | WriteThread::AwaitState、MemTable |
| **WAL fsync（内核态）** | **~10%** | fdatasync/fsync |
| Raft 日志 | ~8% | WAL 编码、appendAsync |
| 内存分配 | ~8% | malloc/sdallocx |
| Schema 查找 | ~3% | getEdgeSchema/getTagSchema |
| folly Future | ~5% | 回调 |

关键：CPU 利用率低（graphd ~11%，storaged ~26%），瓶颈是 I/O（WAL fsync + compaction）。
不要用“加 worker/加 CPU”解决；优先看磁盘、WAL、compaction、批量写入。

## 7. x86 与 ARM 的差异

同一套场景在 AMD EPYC（768MB L3、双路、单 NVMe）与鲲鹏 950 上对比：

| 维度 | x86 表现 | ARM 表现 | 解释 |
|------|---------|---------|------|
| QPS | 全面更高（+3% ~ +48%） | 基准 | x86 IPC/L3/哈希表效率更高 |
| Go2/Go3 内存分配 | ~13–16% | ~22–42% | 大 L3 提升 jemalloc/RocksDB 命中 |
| RocksDB 查询 | 更低 | 更高 | 大 L3 缓存 SST block/index |
| BFS 哈希 | ~3.8% | ~15% | 同一容器实现（robin_hood/unordered_multimap），x86 大 L3 使节点式哈希的 cache miss 代价更低 |
| Insert nGQL 解析 | ~9.7% | ~22% | x86 IPC 更高 |
| 写入 I/O | 单盘双 storaged 竞争，WriteThread 等待更高 | 双盘独立，fsync 开销更明显 | 磁盘布局差异 |
| 安全开销 | SRSO 缓解 ~1% | 无 | x86 特有 |

结论：瓶颈位置一致（Go1 graphd futex、Go2/3 storaged RocksDB、FSP graphd BFS、Insert I/O），
但占比不同。跨平台不要直接套用百分比，方法可复用。

## 8. 扩展实验：多 graphd / 多 storaged

### 2 graphd → 4 graphd（Go1）

| 指标 | 2 graphd | 4 graphd | 变化 |
|------|---------:|---------:|-----:|
| 总 QPS | 189,679 | 273,249 | +44% |
| graphd futex 占比 | 59.4% | 20.9% | -38.5pp |
| graphd folly Future | 2.5% | 17.6% | +15.1pp |
| graphd 内存分配 | 11.5% | 17.3% | +5.8pp |
| storaged CPU | ~2,900% | ~4,400% | +52% |

迁移：futex 被拆散后，Future 回调链、内存分配、MetaClient 缓存成为新热点；
storaged 承受更高 RPC 并发，成为新限制。继续加 VU 只增加排队，QPS 平台化。

### 2 storaged → 4 storaged

- Go2/Go3 的 RocksDB 与 schema 拷贝负载被分摊，QPS 提升；但每个 storaged 仍需足够读池。
- 深查询（Go3）的成功率对读池容量敏感；读池不足时高 VU 会出现失败/长尾。
- 增加 storaged 也增加 graphd 的 RPC 扇出与内存，需整体评估。

### 绑核 vs 浮动

| 场景 | 全浮动 | 固定绑核 | 说明 |
|------|--------|---------|------|
| Go1 点查 | 315K / 93.9% | 375K / 76.8% | 绑核 cache 局部性更好 |
| Go2 多跳 | 18.0K / 97.6% | 11.5K / 52.8% | 固定绑核读池不足，明显变差 |
| Go3 重读 | 216 / 97.6% | 105 / 46.8% | 同上 |
| FSP | 12.2K / 98.0% | 7.4K / 54.5% | graphd 静态核不足 |
| Insert | 36.2K | 27.8K | 写 worker 核不足 |

规律：绑核收益 = cache 局部性，代价 = 失去跨场景弹性。混合负载优先浮动；固定点查型
负载可绑核。任何方案都要用同一压测口径验证。

## 9. Topdown 摘要（实测）

| 场景 | graphd IPC | 主要 stall | storaged 特征 |
|------|-----------|-----------|---------------|
| Go1Step | ~0.52 | Back-End/Memory Bound 高，futex 导致低 IPC | RocksDB 命中较高 |
| Go2Step | 中等 | RPC 等待 + 内存 | RocksDB + GetNeighbors |
| Go3Step | ~2.6（graphd 反而高） | 计算密度高 | RocksDB 扫描主导 |
| FSP | 中等 | 哈希/字符串/内存 | RocksDB + 组装 |
| Insert | 中等 | 前端/解析 | I/O bound，MapQ stall 高 |

注意：graphd IPC 低不等于 graphd 计算重，可能是 futex/等待；要结合线程状态。

## 10. 场景到优化杠杆的映射

| 场景 | 首选杠杆 | 次选 | 不推荐 |
|------|---------|------|--------|
| Go1Step | 增加 graphd 实例/减少单实例 worker | 对象池、schema 缓存 | 继续加 worker |
| Go2Step | 增加 storaged/读池 | 减少拷贝、RocksDB 调优 | 只加 graphd |
| Go3Step | 增加 storaged + 限制扇出/批量化 | RocksDB/内存优化 | 只加 VU |
| FSP | BFS 容器/拷贝消除 | 减少 Value 字符串分配 | 加 worker |
| Insert | WAL/compaction/磁盘/批量 | 预编译/减少解析 | 加 CPU worker |

具体实施与实测收益见 `nebula-performance-optimization`。
