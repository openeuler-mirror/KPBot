---
name: sra-dag-tuning
description: 鲲鹏搜推DAG场景全栈优化方法总表与分类使能指南
---

# 鲲鹏搜推 DAG 全栈优化方法

适用：鲲鹏(ARM/AArch64)平台上搜推(召回/粗排/精排/重排)DAG场景的性能优化。按"总表选方法 → 对应章节看使能方式"使用。每个方法按 使用场景 / 使能方法 / 预期收益 三段组织。

## 优化方法总表

| # | 层级 | 优化方法 | 使用场景 | 预期收益 |
|---|---------|---------|---------|---------|
| 1.1 | 业务/DAG | 关键路径并行化 | 多路召回、特征拉取等无依赖节点串行执行 | RT -20%~-50%（视串行占比） |
| 1.2 | 业务/DAG | batch 合并打分 | 粗排/精排候选逐条打分 | 吞吐 +30%~100% |
| 1.3 | 业务/DAG | 流水线化 | 长节点内部取数与计算串行 | RT -10%~-30% |
| 1.4 | 业务/DAG | 缓存与预计算 | 画像/embedding/热item特征重复查询 | 热点节点耗时 -50%~90% |
| 1.5 | 业务/DAG | cache友好数据结构 | 链表/树遍历、指针追逐、AoS布局 | 访存热点 -30%~60% |
| 1.6 | 业务/DAG | top-k 堆选择替代全排序 | 大候选集(数百~万)只要前K个 | 排序热点 -50%~70% |
| 1.7 | 业务/DAG | 线程池替代每请求建线程 | 请求内动态创建工作线程 | 消除1%~5%调度开销+尾延迟 |
| 1.8 | 业务/DAG | 零拷贝/序列化削减 | 节点间特征深拷贝、protobuf多次序列化 | 相关节点耗时 -20%~50% |
| 1.9 | 业务/DAG | 锁粒度优化 | 全局大锁、锁竞争进perf热点 | 锁热点 -50%~90% |
| 1.10 | 业务/DAG | 异步IO替代同步阻塞 | read/write每秒数百次以上、select轮询 | IO等待不再阻塞会话线程 |
| 1.11 | 业务/DAG | DAG执行路径重构 | 无依赖计算串在链路上、过滤做晚了、上报耦合主链路 | 案例：整体RT -60%，可用性1个9→4个9 |
| 1.12 | 业务/DAG | 并发任务预判剪枝 | 并发子任务队列高峰期过长 | 高峰RT稳定，避免调度雪崩 |
| 1.13 | 业务/DAG | 模型结构-性能联合优化 | 固定RT预算下模型过重或特征冗余 | 同RT效果提升，或同效果RT -30%~50% |
| 1.14 | 业务/DAG | 向量索引与量化选型 | 召回向量检索内存/延迟超预算 | 内存 -80%以上，延迟可控 |
| 1.15 | 业务/DAG | embedding查表去重+热度cache | 打分时重复embedding查表、查表是RT大头 | 查表次数 -50%~90% |
| 1.16 | 业务/DAG | 多路归并分数归一化 | 多路召回分数尺度不一、单路垄断 | 同量级下召回覆盖度提升 |
| 1.17 | 业务/DAG | 粗排双塔/三塔选型与缓存 | 粗排候选万级、交互模型扛不住 | 单条打分毫秒级->微秒级 |
| 1.18 | 业务/DAG | RPC框架调优 | brpc/gRPC服务QPS上不去、长尾大 | 吞吐线性扩展，长尾收窄 |
| 1.19 | 业务/DAG | 长尾延迟治理 | 扇出大的DAG请求P99被最慢路拖垮 | 大扇出下P99数倍改善 |
| 1.20 | 业务/DAG | 在线特征存储调优 | Redis/特征库读延迟超标 | 亚毫秒级特征读取 |
| 1.21 | 业务/DAG | 模型量化与算子融合 | 精排DNN计算是RT大头 | FP16约2倍、INT8约3~4倍吞吐 |
| 1.22 | 业务/DAG | MMR/DPP多样性工程化 | 重排相似度计算O(n²)超时 | 重排耗时控制在毫秒级 |
| 1.23 | 业务/DAG | 规则打散引擎配置化 | 打散/扶持规则频繁变更 | 新规则分钟级上线 |
| 1.24 | 业务/DAG | 混排分数融合与延迟预算 | 多业务分数不可比、RT预算不同 | 整体RT稳定在预算内 |
| 2.1 | 基础库 | KML/NEON加速库 | 数学运算、memcpy/CRC/压缩热点 | 对应热点 -30%~80% |
| 2.2 | 基础库 | 高性能hash map | 特征查表、KV查询热点 | 查表热点 -20%~50% |
| 2.3 | 基础库 | 毕昇JDK | Java搜推服务 | 整体 +5%~25% |
| 2.4 | 基础库 | 华为优化基础库 | glibc/zlib/zstd/snappy/hyperscan是热点 | 对应热点 -20%~60% |
| 2.5 | 基础库 | 高性能正则/字符串匹配 | 正则/hyperscan匹配是热点 | 匹配热点 -30%~70% |
| 2.6 | 基础库 | 压缩算法选型 | RPC/日志/快照压缩CPU或带宽超标 | CPU降50%以上或带宽省3倍 |
| 3.1 | 编译器 | 毕昇编译器+优化flag | 所有C/C++服务 | 整体 +5%~15% |
| 3.2 | 编译器 | PGO | 分支/布局敏感的热点代码 | 额外 +5%~15% |
| 3.3 | 编译器 | LSE原子指令 | 多线程同步/原子操作热点 | 多线程同步性能显著提升 |
| 3.4 | 编译器 | cacheline对齐防伪共享 | 多核写相邻变量、LLC一致性开销大 | 热点消除，吞吐回升 |
| 3.5 | 编译器 | 分支预测友好改写 | mispredicts/branch-mpki高的热点 | 热点 -10%~30% |
| 3.6 | 编译器 | 自动向量化检查与循环改写 | 热点循环无NEON指令 | 热点循环4~8倍 |
| 3.7 | 编译器 | 循环不变量外提与强度削弱 | 循环内重复乘除计算 | 算术热点 -10%~30% |
| 3.8 | 编译器 | 函数内联与调用图优化 | 小函数调用频繁/虚调用热点 | 热点 -5%~20% |
| 3.9 | 编译器 | LTO链接期优化 | 跨编译单元调用无法内联 | 整体 +2%~8% |
| 3.10 | 编译器 | 代码布局优化 | 前端bound/icache miss高 | 前端热点 -10%~25% |
| 4.1 | OS | NUMA绑核/关平衡 | 多NUMA节点服务器(鲲鹏标配) | RT -10%~30%，尾延迟显著改善 |
| 4.2 | OS | 大页 | 大索引/特征库常驻内存 | TLB miss相关热点 -10%~40% |
| 4.3 | OS | 内核隔离与中断亲和 | 高QPS低延迟服务 | P99 -10%~30% |
| 4.4 | OS | 网卡Offload与多队列 | 内核do_csum等协议栈热点、中断集中单核 | CPU协议栈开销大幅下降 |
| 4.5 | OS | 异步批量IO | 文件读写每秒>500次阻塞调用 | IO吞吐提升且不阻塞线程 |
| 4.6 | OS | 网络sysctl调优 | SYN溢出、TIME_WAIT堆积、包丢弃 | QPS上限抬高 |
| 4.7 | OS | 网卡ring buffer与中断合并 | NET_RX集中、洪峰丢包 | 洪峰丢包消除 |
| 4.8 | OS | 磁盘IO调度器与文件系统 | SSD上CFQ多余、小文件元数据开销 | NVMe随机IO延迟 -10%~30% |
| 4.9 | OS | THP策略与numad | THP defrag造成P99尖刺 | 消除毫秒级尖刺 |
| 4.10 | OS | cgroup/容器资源隔离 | 混部邻居抢占CPU/LLC | 业务P99稳定 |
| 5.1 | 芯片/BIOS | BIOS基线 | 新机部署基线检查 | 带宽可达2倍差距，必查项 |
| 5.2 | 芯片/BIOS | PCIe同NUMA侧 | 网卡/SSD跨node访问 | 跨node IO延迟 -30%~50% |
| 5.3 | 芯片/BIOS | CPU频率性能模式 | 默认ondemand/降频 | 整体 +10%~30% |
| 5.4 | 芯片/BIOS | KAE硬件加速引擎 | SSL/TLS加解密、数据压缩CPU占用高 | RSA异步签名提升最高74倍 |
| 5.5 | 芯片/BIOS | L3分区模式 | L3延迟敏感型业务 | L3延迟约36周期(近4MB内) |
| 5.6 | 芯片/BIOS | BIOS内存配置项 | 装机检查/Die交织错误配置 | 避免访存显著变慢 |
| 5.7 | 芯片/BIOS | CPU预取开关 | 按访问模式取舍预取 | 顺序负载延迟隐藏 |
| 5.8 | 芯片/BIOS | CPPC调频模式 | 动态调频与固定频率取舍 | 突发响应或功耗优化 |
| 5.9 | 芯片/BIOS | 内存带宽基线检查 | 带宽型负载不达理论值 | 发现少插内存条类问题 |
| 5.10 | 芯片/BIOS | PCIe拓扑与AF_XDP | 高PPS内核栈瓶颈、跨node | PPS提升数倍 |
| 6.1 | 通用 | 预热/降级/背压 | 上线冷启动、超载保护 | 尾延迟稳定性 |
| 6.2 | 通用 | 分级存储 | 特征库超内存容量 | 成本可控下保热数据性能 |
| 6.3 | 通用 | Hyper Tuner自动分析 | 无明确热点方向、系统性体检 | 自动生成优化建议清单 |
| 6.4 | 通用 | 火焰图on/off-CPU分析 | P99尖刺均值正常、定位不出根因 | 精确定位CPU忙/等两类根因 |
| 6.5 | 通用 | 容量与限流基线 | 压测不准、QPS水位无上限 | 过载不雪崩，容量有据可依 |
| 6.6 | 通用 | Tuned自动配置档 | 不想逐项手调OS参数 | 一条命令获得基线 |
| 6.7 | 通用 | 中断与软中断监控 | P99抖动但CPU不高 | 快速定位中断集中核 |
| 6.8 | 通用 | 压测流量回放与影子验证 | 优化需真实流量背书 | 上线风险可控 |
| 6.9 | 通用 | 性能回归看板与告警 | 优化成果随迭代回退 | 性能纳入CI门禁 |
| 6.10 | 通用 | 效果归因方法论 | 多项优化说不清贡献 | 每项收益有据可查 |

## 1、业务/DAG层

### 1.1 关键路径并行化

- 使用场景：DAG中无依赖的兄弟节点(多路召回、多特征源拉取)被串行调度。
- 使能方法：

```cpp
// 分层调度：同层节点提交线程池后屏障等待
void DagEngine::RunLevel(int level, Session& s) {
    std::atomic<int> remaining(nodes_[level].size());
    std::promise<void> done;
    for (auto* node : nodes_[level]) {
        pool_->Submit([&, node] {
            node->Execute(s);                     // 节点只依赖上游已完成节点
            if (--remaining == 0) done.set_value();
        });
    }
    done.get_future().wait();                     // 本层全部完成才进下一层
}

// 多路召回散射-聚合，统一超时，超时路降级跳过
std::vector<RecallResult> MultiRecall(Session& s, const Request& req) {
    std::vector<std::future<RecallResult>> futs;
    for (auto* channel : recall_channels_)
        futs.push_back(pool_->Submit([&, channel] { return channel->Search(req); }));
    std::vector<RecallResult> all;
    for (auto& f : futs) {
        auto r = f.get();
        if (!r.timeout) all.insert(all.end(), r.items.begin(), r.items.end());
    }
    return all;
}
```

- 预期收益：RT -20%~-50%（视串行占比）；关键路径收益 = sum(串行RT) - max(各路RT)。

### 1.2 batch 合并打分

- 使用场景：粗排/精排对候选逐条调用打分接口（每条一次kernel/算子launch）。
- 使能方法：候选攒批后单次批量打分；行计算转列计算(按特征列连续存储)，配合SIMD一次处理整列。参考阿里COLD。

```cpp
// 反例：逐条打分
for (auto* item : cands) scores[i] = mlp(item->features);   // 每条一次launch

// 正例：列存储 + 单次批量GEMM（KML blas，见2.1）
void BatchScore(const float* col_major_feats, int n_items, int n_feats,
                const float* w, float* out) {
    cblas_sgemv(CblasRowMajor, CblasNoTrans,
                n_items, n_feats, 1.0f, col_major_feats, n_feats,
                w, 1, 0.0f, out, 1);
}
```

- 预期收益：吞吐 +30%~100%。

### 1.3 流水线化

- 使用场景：节点内部"取数据→处理"串行循环（如召回边取边算、特征边拉边算）。
- 使能方法：预取线程提前拉下一批数据，与当前计算重叠；双缓冲队列衔接。

```cpp
void PipelinedFetchCompute(Loader& loader, Compute& compute) {
    BoundedQueue<Batch> q(2);                      // 双缓冲
    std::thread producer([&] {
        while (running) q.Push(loader.Next());     // 预取下一批
    });
    while (auto batch = q.Pop())
        compute.Run(batch);                        // 计算与取数重叠
    producer.join();
}
```

- 预期收益：节点总耗时 ≈ max(取数,计算) 而非两者之和，RT -10%~-30%。

### 1.4 缓存与预计算

- 使用场景：用户画像、item embedding、热门特征每请求重复查询或实时计算。
- 使能方法：热数据本地缓存(进程内hash/LRU，实现见1.4补充)；可预计算的离线算好存表；新item向量缺失时本地预估并缓存；监控命中率与新鲜度。

```cpp
template <class K, class V>
class ShardedLru {
    std::vector<std::unique_ptr<LruShard<K,V>>> shards_;   // 槽数=2的幂
    LruShard<K,V>& Pick(const K& k) { return *shards_[hash(k) & (shards_.size()-1)]; }
public:
    V* Get(const K& k)        { return Pick(k).Get(k); }
    void Put(const K& k, V v) { Pick(k).Put(k, std::move(v)); }
};
```


**补充（分槽+读写锁/FIFO 实现，读多写少命中率>95%场景）**：

```cpp
class LruShard {
    mutable std::shared_mutex mu_;
    std::list<Entry> fifo_;                        // FIFO即可，无需移到头部
    std::unordered_map<K, typename std::list<Entry>::iterator> map_;
public:
    V* Get(const K& k) {
        std::shared_lock rd(mu_);                  // 读共享，吞吐数倍于mutex
        auto it = map_.find(k);
        return it == map_.end() ? nullptr : &*it->second->value;
    }
    void Put(const K& k, V v) {
        std::unique_lock wr(mu_);
        if (fifo_.size() >= Cap()) { map_.erase(fifo_.front().key); fifo_.pop_front(); }
        fifo_.push_back({k, std::move(v)});
        map_[k] = std::prev(fifo_.end());
    }
};
// 实测案例：单mutex读50次最坏5ms -> 分槽+读写锁后P99 <0.1ms
```
- 预期收益：热点节点耗时 -50%~90%。

### 1.5 cache友好数据结构

- 使用场景：perf显示访存热点（LLC miss高）、链表/树指针追逐、AoS布局浪费带宽。
- 使能方法：SoA替代AoS；数组/连续存储替代链表；热字段单独紧凑排布（如倒排的HashMap索引+短链连续存储+RCU，参考百度HIT）；多核下避免CAS自旋，用业务分片打散。

```cpp
// AoS -> SoA：读ctr不再拉全缓存行
struct ItemsAoS { int id; float ctr; float price; char tag[16]; };   // 32B
struct ItemsSoA { std::vector<int> id; std::vector<float> ctr; std::vector<float> price; };

// 倒排：HashMap定位 + docid连续短链（预取友好）+ RCU更新
struct PostingList {
    std::vector<uint32_t> docids;
    std::vector<float>    scores;
};
// 多核CAS自旋 -> 按 docid%N 分片，各线程只写自己的片，无竞争
```

- 预期收益：访存热点 -30%~60%。

### 1.6 top-k 堆选择替代全排序

- 使用场景：合并后候选集(数百~万条)只需要top-K送下游，却做了全量排序。下游需要全序时才保留全排序。
- 使能方法：固定大小K的小顶堆，单趟扫描候选，score≤堆顶跳过，否则替换堆顶下沉。O(n)替代O(n·log n)，且避免qsort对大结构体的反复memcpy。

```cpp
// 场景实测：1200条×160B qsort占47% CPU -> 小顶堆降到14%
void TopK(const Item* items, int n, int k, std::vector<const Item*>& out) {
    std::priority_queue<Item, std::vector<Item>, ScoreGreater> heap;  // 小顶堆
    for (int i = 0; i < n; ++i) {
        if ((int)heap.size() < k) { heap.push(items[i]); continue; }
        if (items[i].score > heap.top().score) { heap.pop(); heap.push(items[i]); }
    }
    out.resize(k);
    for (int i = k - 1; i >= 0; --i) { out[i] = new Item(heap.top()); heap.pop(); }
}
```

- 预期收益：排序热点 -50%~70%。

### 1.7 线程池替代每请求建线程

- 使用场景：请求处理中pthread_create动态建线程，开销出现在perf热点且加剧抖动。
- 使能方法：进程启动时建固定线程池(大小=绑核后核数，见4.1)，请求内任务提交到池。

```cpp
class FixedPool {
    std::vector<std::thread> workers_;
    BlockingQueue<std::function<void()>> tasks_;
public:
    explicit FixedPool(int n) {
        for (int i = 0; i < n; ++i)
            workers_.emplace_back([this] { while (auto t = tasks_.Pop()) (*t)(); });
    }
    void Submit(std::function<void()> f) { tasks_.Push(std::move(f)); }
};
```

- 预期收益：消除1%~5%调度开销，尾延迟波动收窄。

### 1.8 零拷贝/序列化削减

- 使用场景：节点间特征用protobuf深拷贝传递、多次序列化/反序列化。
- 使能方法：节点间传指针/引用计数；跨进程用flatbuffers（零反序列化访问）；大特征块共享内存。

```cpp
// protobuf arena：会话级Arena，节点间传Message*，不再深拷贝
google::protobuf::Arena arena;
auto* feats = google::protobuf::Arena::CreateMessage<FeaturePb>(&arena);
node_a->Fill(feats);                 // 指针直接传给下游
node_b->Consume(feats);              // 无copy
```


**补充（对象池/内存池，分配开销进perf热点时）**：

```bash
export LD_PRELOAD=/usr/lib64/libjemalloc.so.2
export MALLOC_CONF="narenas:2,dirty_decay_ms:10000,muzzy_decay_ms:10000"
```

```cpp
class SessionPool {
    std::vector<Session*> free_;
    std::mutex mu_;
public:
    Session* Acquire() { std::lock_guard l(mu_);
        if (free_.empty()) return new Session;
        auto* s = free_.back(); free_.pop_back(); return s; }
    void Release(Session* s) { s->Reset(); std::lock_guard l(mu_); free_.push_back(s); }
};
// 会话级大对象走对象池(复用前完整重置); protobuf用arena(上方); 任务级用bthread类m:n协程池
```

**补充（协议二进制化+压缩，HTTP/JSON/文本协议传大特征包时）**：升级二进制RPC（brpc/gRPC，内网也值得），大包加snappy/lz4压缩。

```cpp
brpc::Controller* cntl = ...;
cntl->set_response_compress_type(brpc::COMPRESS_TYPE_SNAPPY);
// 预期：传输耗时-30%~60%，带宽大降
```
- 预期收益：相关节点耗时 -20%~50%。

### 1.9 锁粒度优化

- 使用场景：perf/contention_profiler显示lock类函数cycles占比>5%，全局大锁在多核下竞争严重。
- 使能方法：大锁拆小锁（按业务分片，每核/每线程独立资源）；减少并发线程数；锁变量cacheline对齐（alignas(64)）防伪共享；读多写少场景换读写锁（见1.4补充）。

```cpp
// 大锁拆分片锁
std::mutex global_mu;                                  // 反例：全核抢一把锁
std::vector<std::mutex> shard_mu(kShards);             // 正例：docid%kShards 分片
auto& mu = shard_mu[key % kShards];

// 锁变量cacheline对齐，防相邻变量伪共享
struct alignas(64) AlignedCounter { std::atomic<int> v{0}; };
```

- 预期收益：锁热点 -50%~90%。

### 1.10 异步IO替代同步阻塞

- 使用场景：read/write/pread64/pwrite64合计每秒>500次调用、select每秒>1000次（Hyper Tuner触发条件），IO阻塞会话线程。
- 使能方法：文件IO换libaio/io_uring异步批量提交；网络事件select换epoll（select上限1024且轮询扫描）。

```cpp
// io_uring批量提交示例（liburing）
io_uring_queue_init(256, &ring, 0);
for (auto& op : batch) {                                // 一次提交一批
    io_uring_prep_read(sqe, op.fd, op.buf, op.len, op.off);
    io_uring_sqe_set_data(sqe, op.ctx);
}
io_uring_submit(&ring);
// epoll替换select: epoll_create/epoll_ctl/epoll_wait
```

- 预期收益：IO等待不再阻塞会话线程，单线程可并发处理多路读写。

### 1.11 DAG执行路径重构

- 使用场景：无依赖计算被串在链路上、去重/过滤做晚了、上报/debug耦合在主链路。收益最大的第一刀。
- 使能方法：

```cpp
// (a) 前置无依赖计算：用户塔预计算，traceid同实例路由
// 中控侧：发召回请求的同时异步下发预计算，网关按traceid一致性哈希路由到同一粗排实例
rpc::AsyncCall(recall_stub,  &Recall::Search,   recall_req, on_recall);
rpc::AsyncCall(prerank_stub, &PreRank::WarmUser, warm_req,  nullptr);   // 与召回并行

// 粗排侧：WarmUser计算用户embedding进实例缓存，完整请求到达直接读
void PreRankImpl::WarmUser(const WarmReq& req, Resp&) {
    user_cache_.Put(req.traceid, ComputeUserEmb(req.user_feats));
}
Status PreRankImpl::FullScore(const FullReq& req, Resp& resp) {
    auto emb = user_cache_.Get(req.traceid);            // 命中则零等待
    if (!emb) emb = ComputeUserEmb(req.user_feats);     // 兜底
}

// (b) 去重/过滤前置：必须在拉embedding/打分之前，否则全做重复功
auto uniq = DedupByItemId(recall_results);      // 先去重
FetchItemEmb(uniq);                             // 再拉特征

// (c) 旁路解耦：样本拼接/debug上报，response之后异步抽样执行
void Serve(Request& req, Resp& resp) {
    /* ...核心链路... */
    if (sampler_.Hit())
        pool_->Submit([req, resp] { reporter_.BuildAndSend(req, resp); });  // 不占会话
}
```

- 预期收益：案例整体RT -60%，可用性1个9→4个9。

### 1.12 并发任务预判剪枝

- 使用场景：DAG节点内部起大量并发子任务（协程/fiber），高峰期任务队列堆积反而拖慢耗时。
- 使能方法：子任务提交前先判断可否跳过，不进队列；实例部署上"减实例加核"（总核数不变，单实例核数增加），降低实例内并发冲突。

```cpp
for (auto* item : cands) {
    if (item->filtered_by_upstream) continue;              // 上游已滤
    if (item->static_score < kFloorScore) continue;        // 静态分必然不达标
    pool_->Submit([item] { ScoreOne(item); });
}
// 部署: 32核×2实例 -> 64核×1实例
```

- 预期收益：高峰RT稳定，避免调度雪崩。

### 1.13 模型结构-性能联合优化

- 使用场景：粗排/精排打分节点是RT大头，单纯工程优化已到顶。需算法团队协同。
- 使能方法（按工程投入从低到高）：
  - COLD范式（阿里）：轻量全连接 + 列计算（见1.2）+ 量化 + SEBlock特征门控裁剪低价值特征列；
  - 知识蒸馏/优势特征蒸馏PFD（淘宝）：精排做teacher蒸馏粗排，模型小但效果逼近精排；
  - NAS/AutoFAS（美团）：在给定RT预算约束下自动搜索最优特征组合+模型结构，把"性能"作为约束而非事后裁剪。

```cpp
// COLD工程部分：SEBlock式特征列门控，逐列过滤低价值特征
void ColdColumnSelect(const std::vector<ColStat>& stats, std::vector<int>& keep) {
    for (int c = 0; c < (int)stats.size(); ++c)
        if (stats[c].importance > kThresh) keep.push_back(c);   // 离线定阈值
}
// 列计算+int8量化配合1.2的sgemv
```

- 预期收益：同RT预算下效果提升，或同效果RT -30%~50%。

### 1.14 向量索引与量化选型

- 使用场景：召回向量检索索引内存超预算（10亿条128维HNSW需约700GB）、或查询延迟超标。
- 使能方法：按内存/召回/延迟三角选型——HNSW内存给足时top10召回99%、p50约23ms；内存受限用IVF+PQ（同规模114GB，但召回降至66%、p50约117ms）或二进制量化BQ/BBQ（1bit+HNSW+3倍过采样召回>90%），BQ检索后加rerank步骤恢复精度；sq8标量量化是延迟损失最小的折中。混合策略：热item用HNSW全精度、长尾item用量化索引。

```bash
# faiss示例：IVF+PQ构建
python -c "
import faiss
quantizer = faiss.IndexFlatL2(128)
index = faiss.IndexIVFPQ(quantizer, 128, 4096, 16, 8)   # 128维, 4096聚类, 16子段8bit
index.train(x_train); index.add(x_all)
index.nprobe = 16                                        # probe数调召回/延迟平衡
"
```

- 预期收益：内存 -80%以上（PQ/BQ），延迟可控；延迟富余换内存时nprobe动态调节。











### 1.15 embedding查表去重+热度cache

- 使用场景：打分时batch内大量重复item引用同一embedding，embedding查表是RT大头。
- 使能方法：查表前先去重（dedup后查、按索引广播回来）；按访问频率把热embedding常驻本地cache（HugeCTR HPS实测95%查表命中10%热key），miss回源全量表；鲲鹏侧embedding表用大页+NUMA绑核（见4.1/4.2）压查表延迟；超大表用层级参数服务器（GPU cache->CPU DRAM->SSD三级，HugeCTR HPS架构）+FP16/INT8量化，显存/内存省一半以上。

```cpp
// batch内去重查表
std::unordered_map<uint64_t, int> uniq;                 // key->首次出现位置
for (auto k : keys) if (!uniq.count(k)) uniq[k] = fetch_emb(k);   // 每key只查一次
```

- 预期收益：查表次数 -50%~90%；层级存储下内存 -50%以上且查表接近全命中。

### 1.16 召回：多路归并分数归一化

- 使用场景：多路召回(倒排/向量/I2I)分数尺度不同，简单归并导致某路垄断topK，其他路白跑。
- 使能方法：各路分数先归一(z-score/min-max)或按路配权重再归并；每路设配额(quota)保底保顶；截断(truncation)每路只取topN进归并，控制下游量级。

```cpp
// 每路截断+配额归并
for (auto& ch : channels) ch.take = TopN(ch.results, ch.truncation_n);
// 归一时按路权重加权: final = w_ch * normalize(score)
// 配额: 每路保底k_min个进粗排, 防止单路垄断
```

- 预期收益：同候选量级下召回覆盖度提升；下游打分量可控。

### 1.17 粗排：三塔/双塔结构选型与缓存

- 使用场景：粗排候选数千~万级，交互式全连接模型(COLD类)扛不住RT时。
- 使能方法：双塔/三塔把item侧离线算好embedding缓存（在线只算user塔+内积，内积用1.2的批量sgemv/SIMD）；user塔预计算前置（见1.11路径重构）；需要交叉特征时用COLD列计算而非全交互DNN；SEBlock门控裁特征（见1.13）。

- 预期收益：粗排单条打分从毫秒级降到微秒级；支持万级候选。

### 1.18 RPC框架调优

- 使用场景：brpc服务QPS随线程数增长 plateau、长尾大、小消息吞吐不足。
- 使能方法：
  - bthread化IO：EventDispatcher只负责事件分发，处理在bthread池做（work stealing），worker线程数≈核数即可，勿开大量IO线程；
  - 多消息分批处理：一次读出n条消息时n-1条起bthread、最后1条原地处理，避免额外调度；
  - wait-free写路径：并发写同连接走MPSC链表+KeepWrite bthread统一writev，高吞吐下自动合批；
  - IOBuf零拷贝切分协议消息，避免memcpy；
  - 连接模式：小消息(<16KB)单连接性能最优；大消息用连接池（可达2.3GB/s）。

```bash
# brpc内置诊断：运行时定位瓶颈
curl http://<ip>:<port>/vars/bthread_count       # bthread数量
curl http://<ip>:<port>/contention_profiler      # 锁竞争
curl http://<ip>:<port>/rpcz                     # 每方法RPC延迟分布
# 关键flag
--bthread_concurrency=48                         # ≈绑核后核数
--socket_max_unwritten_bytes=8388608             # 控制合批上限
```

- 预期收益：吞吐随线程数线性扩展至256线程（多数RPC框架8~16线程即饱和），长尾收窄。

### 1.19 长尾延迟治理

- 使用场景：DAG大扇出（并行调几十上百个下游）时，即使单下游P99.99达标，整请求仍有大比例超时——0.01%单点慢×100并行≈1%请求被拖住。
- 使能方法（Google《The Tail at Scale》）：
  - 服务分级+优先级队列：在线交互请求优先调度，非交互/后台任务降级排队；
  - 拆长请求防队头阻塞：大请求切成小请求与其他短任务交错执行；
  - hedge request对冲：单路慢过阈值时向副本发重复请求，取先返回者；
  - 金丝雀请求：大扇出前先发1~2个叶子验证，防未测代码路径同时打爆全部实例；
  - 微分区/后台活动错峰：GC、快照等后台任务与流量错峰，中断绑到非业务核（见4.3）。

```cpp
// hedge request（对冲请求）骨架
void HedgedCall(Stub& s, const Req& req, Callback done) {
    auto t0 = Now();
    s.AsyncCall(req, [&, t0](Resp r1) {
        if (Now() - t0 < kDeferMs) { done(r1); return; }        // 快路径
        second_channel.AsyncCall(req, [done, m1 = std::move(r1)](Resp r2) mutable {
            done(r2.ok ? r2 : m1);                              // 取先返回的有效结果
        });
    });
    // kDeferMs = 该下游P95左右；需下游幂等
}
```

- 预期收益：大扇出场景P99数倍改善；成本是多发少量冗余请求。

### 1.20 在线特征存储调优

- 使用场景：Redis/自研特征库读延迟超标、特征行组装慢。
- 使能方法：
  - 两级hash建模：一级key=实体(user_id)，二级field=特征表/特征名，一次HGETALL取整行，避免多次RTT；
  - pipeline/MGET批量拉多实体特征，网络RTT合1；
  - TTL自动过期防陈旧特征（如leads 48h过期自动清理）；
  - gRPC+Java客户端在线服务路径比Python客户端快约30%（Feast实测）；
  - 特征预组装：线上按"特征向量"整体存取（pack成二进制blob），不在请求时逐列拼；
  - 分级内存（Redis Enterprise Flash/NVMe）扩容不降热数据性能。

```bash
# 批量pipeline取特征（redis-cli示意）
redis-cli --pipe <<EOF
HGETALL feat:user:12345
HGETALL feat:item:67890
EOF
# 生产代码: redis-py pipeline / brpc redis客户端, MGET实体行
```

- 预期收益：亚毫秒级特征读取；RTT次数从N次降为1次。

### 1.21 精排：模型量化与算子融合（昇腾/鲲鹏推理引擎）

- 使用场景：精排DNN计算是RT大头，FP32推理浪费。
- 使能方法：INT8/FP16量化（分坑位混合精度：对精度敏感的层保留FP16）；算子融合（Conv+ReLU垂直融合、QKV水平融合）减kernel launch与访存；NVIDIA侧TensorRT/华为侧CANN atc转模型自动做图优化+融合；鲲鹏纯CPU侧用1.2列计算+KML sgemv。

```bash
# 昇腾: atc模型转换(自动图优化+算子融合+量化)
atc --model=rank.onnx --framework=5 --soc_version=<Ascend型号> \
    --output=rank_fp16 --precision_mode=allow_fp32_to_fp16
# NVIDIA: trtexec --onnx=rank.onnx --int8
```

- 预期收益：FP16约2倍、INT8约3~4倍推理吞吐（精度需A/B验证）。

### 1.22 重排：MMR/DPP多样性打分工程化

- 使用场景：重排需要兼顾相关性与多样性，MMR/DPP朴素实现O(n²)相似度计算在候选大时超时。
- 使能方法：MMR只对滑动窗口内已选item算相似度（窗口W=8~16，复杂度从O(n²)降到O(n·W)）；DPP用Fast Greedy MAP(贪心行列式更新O(k³))或小红书SSD式Gram-Schmidt不建核矩阵；item embedding预先归一化，相似度=内积走KML blas；类目/作者打散用规则引擎(窗口计数)与MMR叠加。

```cpp
// 滑窗MMR骨架
for (int step = 0; step < K; ++step) {
    double best = -inf; Item* pick = nullptr;
    for (auto* c : candidates) {
        double sim = MaxSim(c, selected_last_W);   // 只比窗口内已选
        double v = lambda * c->score - (1 - lambda) * sim;
        if (v > best) { best = v; pick = c; }
    }
    selected.push_back(pick); UpdateWindow(pick);
}
```

- 预期收益：重排节点耗时可控在毫秒级；多样性指标(类目覆盖/ILAD)提升。

### 1.23 重排：规则打散引擎配置化

- 使用场景：类目打散、同作者限流、新品扶持等业务规则频繁变更，硬编码每次都要发版。
- 使能方法：规则抽象成(维度,窗口,阈值,动作)配置项下发，规则引擎在重排阶段统一执行；上下文相关规则(分时段/分人群)支持动态覆盖。

```json
{"rules": [
  {"dim": "category", "window": 5, "max": 2, "action": "push_back"},
  {"dim": "author",   "window": 10, "max": 1, "action": "skip"}
]}
```

- 预期收益：新规则上线分钟级；避免规则相互冲突导致的顺序抖动。

### 1.24 混排：多业务分数融合与延迟预算分配

- 使用场景：多业务(广告/自然/内容)混排时分数不可比、且各业务RT预算不同。
- 使能方法：各业务分标校准(如校准到CTR尺度)后统一价值公式融合；按时延预算拆分DAG——给每路设deadline，超时路降级(返回部分结果)不阻塞整体（配合1.16长尾治理）；算力动态分配：高价值场景给精排更多候选额度，低价值场景收窄（美团动态算力分配实践）。

- 预期收益：整体RT稳定在预算内；单位算力收益提升。



1. 先用perf定位热点，按总表匹配方法，禁止无数据盲目套用。
2. 每项优化A/B验证：同压测下交替运行取中位数（连续单跑同机波动可达±40%），对比QPS/RT(P99)+归因。
3. 一次只改一个变量。
4. 结构性收益大于微调：执行路径重构(1.11)、过滤前置、旁路解耦这类"少做事"的优化，通常大于单点加速"把事做快"。

## 2、基础库层

### 2.1 KML/NEON加速库

- 使用场景：数学运算(BLAS/FFT)、memcpy、CRC、压缩是热点。
- 使能方法：华为KML替代MKL/OpenBLAS；BoostKit加速库直接替换glibc调用；自定义热点kernel用NEON intrinsics重写。

```bash
yum install biolibkml biolibkml-devel   # openEuler/麒麟仓库
g++ ... -L/usr/local/kml/lib -lkml -lkmlio
```

```cpp
// NEON int8点积（批量打分量化后）
#include <arm_neon.h>
float DotInt8(const int8_t* a, const int8_t* b, int n) {
    int32x4_t acc = vdupq_n_s32(0);
    for (int i = 0; i < n; i += 16) {          // n按16对齐
        int8x16_t va = vld1q_s8(a + i), vb = vld1q_s8(b + i);
        int16x8_t lo = vmull_s8(vget_low_s8(va),  vget_low_s8(vb));
        int16x8_t hi = vmull_s8(vget_high_s8(va), vget_high_s8(vb));
        acc = vpadalq_s16(acc, lo); acc = vpadalq_s16(acc, hi);
    }
    return (float)(vaddvq_s32(acc));
}
```

- 预期收益：对应热点 -30%~80%（memcpy 4K +30%、CRC32C +30%）。

### 2.2 高性能hash map

- 使用场景：特征/embedding查表是热点（std::unordered_map性能弱）。
- 使能方法：换robin_hood/phf等cache友好实现；极热点KV加进程内缓存。

```cpp
#include <robin_hood.h>
robin_hood::unordered_map<uint64_t, ItemEmb> emb_table_;
// phf用于只读极热点KV（完美hash，离线构建）
```

- 预期收益：查表热点 -20%~50%。

### 2.3 毕昇JDK

- 使用场景：Java技术栈的搜推服务。
- 使能方法：换毕昇JDK，开启AppCDS、调优G1/ZGC、Vector API向量化热点。

```bash
java -XX:+UseAppCDS -Xshare:auto ...              # AppCDS加速启动预热
java -XX:+UseG1GC -XX:MaxGCPauseMillis=10 ...     # 低延迟GC目标
# Vector API（JDK17+预览）向量化热点计算
```

- 预期收益：整体 +5%~25%（官方数据Hive/Spark 2%~25%）。

### 2.4 华为优化基础库

- 使用场景：glibc/zlib/zstd/snappy/hyperscan等基础库函数出现在perf热点。
- 使能方法：直接替换华为为鲲鹏优化的版本（NEON向量化，无需改代码）；编译链接时指定即可。

```bash
# 华为鲲鹏优化版基础库（github.com/kunpengcompute）
# gzip 1.10 / zstd 1.4.4 / snappy 1.1.7 / hyperscan 5.2.1 / glibc 2.31
git clone https://github.com/kunpengcompute/zstd && cd zstd && make -j && make install
# 验证: LD_DEBUG=libs确认加载新库; perf中对应热点下降
```

- 预期收益：对应热点 -20%~60%。

### 2.5 高性能正则/字符串匹配

- 使用场景：敏感词过滤、query理解、特征规则匹配中正则引擎是热点。
- 使能方法：std::regex换成Hyperscan（鲲鹏优化版，SIMD并行匹配，流式+多模式一次编译匹配万级规则）；简单字符串匹配用memmem/SIMD strchr批量。

```bash
git clone https://github.com/kunpengcompute/hyperscan && cd hyperscan
cmake -DBUILD_SHARED_LIBS=on .. && make -j && make install
```

- 预期收益：匹配热点 -30%~70%（万级多模式可保持GB/s级吞吐）。

### 2.6 压缩算法选型

- 使用场景：RPC大包/日志/快照的压缩解压CPU超标或带宽不够。
- 使能方法：按速度/比率选型——LZ4压缩500+MB/s解压3+GB/s（速度优先，网络协议/实时日志）；zstd（均衡，各level可调）；小payload(<4KB)用zstd字典压缩提2~5倍比率；鲲鹏上直接换华为优化版或KAE硬件卸载（见5.4/2.4）。

```bash
# 实测定级: 按自己数据压一轮再选
zstd -b1e5 /path/to/sample.bin          # zstd自带benchmark
lz4bench                                 # lz4对比
```

- 预期收益：同带宽CPU降50%以上，或同CPU带宽省3倍。

## 3、编译器层

### 3.1 毕昇编译器+优化flag

- 使用场景：所有C/C++服务。
- 使能方法：毕昇编译器(LLVM系，针对鲲鹏微架构)或GCC≥10；`-O3 -march=armv8.2-a+crypto+fp16 -flto`（按CPU特性裁剪）。检查热点循环是否自动向量化，未向量化手动改写。

```bash
yum install bisheng-compiler
optc/optcxx -O3 -march=armv8.2-a+crypto+fp16 -flto -funroll-loops ...

# 检查热点循环是否自动向量化
optcxx -O3 -march=armv8.2-a -fopt-info-vec-missed loop.cpp   # 列出未向量化原因
objdump -d binary | grep -cE '\s(vld|fmla|mla)\s'            # 数NEON指令
```

- 预期收益：整体 +5%~15%。

### 3.2 PGO

- 使用场景：热点代码分支密集、布局敏感。
- 使能方法：插桩编一版→灌真实流量采集→重编。

```bash
optcxx -O3 -fprofile-generate -g -fno-omit-frame-pointer -o server_inst server.cpp
./server_inst                      # 灌真实/仿真流量，生成 *.profraw
llvm-profdata merge -output=server.profdata *.profraw
optcxx -O3 -fprofile-use=server.profdata -o server server.cpp
# 注意：压测二进制保留 -g -fno-omit-frame-pointer 维持perf归因；
# 关键函数被内联吞掉时 __attribute__((noinline)) 保符号
```

- 预期收益：额外 +5%~15%。

### 3.3 LSE原子指令

- 使用场景：多线程同步/原子操作是热点（ARMv8.1前atomic用LL/SC循环实现，高竞争下退化）。
- 使能方法：编译加 `-march=armv8.1-a`（或含+lse），编译器自动把atomic操作换成LSE单指令（LDADD/CAS等），openGauss用此技术显著提升多线程同步与Xlog写入性能。

```bash
g++ -O3 -march=armv8.1-a ...            # 或 -march=armv8.2-a（鲲鹏920基线，含LSE）
# 确认CPU支持: lscpu | grep -o lse
```

- 预期收益：多线程同步性能显著提升，原子热点下降。

### 3.4 cacheline对齐防伪共享

- 使用场景：多核各自高频写相邻的变量（计数器、标志位），LLC一致性流量大、 cycles高但逻辑简单。
- 使能方法：热点写变量按cacheline对齐隔离；结构体布局把"同线程写"字段聚在一起、"跨线程读"字段分离。

```cpp
// 伪共享反例：两个线程分别写 a/b，但二者同cacheline
struct Bad { std::atomic<int> a; std::atomic<int> b; };
// 正例：64字节对齐隔离
struct Good {
    alignas(64) std::atomic<int> a;
    alignas(64) std::atomic<int> b;
};
```

- 预期收益：伪共享热点消除，多核吞吐回升。

### 3.5 分支预测友好改写

- 使用场景：perf显示branch-misses/branch-mpki高的热点（鲲鹏920的TaiShan v110核在分支密集负载如505.mcf上分支预测弱于Neoverse N1，需要软件补偿）。
- 使能方法：[[likely]]/[[unlikely]]标注常见路径；switch按case频率排布；数据驱动的分支改为查表/谓词；PGO（3.2）自动做布局优化。

```cpp
if (item [[likely]] -> cached) { ... }       // C++20
// 或编译器内置: __builtin_expect(cond, 1)
```

- 预期收益：分支密集热点 -10%~30%。


### 3.6 自动向量化检查与循环改写

- 使用场景：热点循环未生成NEON指令（objdump看不到vld/fmla），白白浪费向量单元。
- 使能方法：`-fopt-info-vec-missed`列出未向量化原因，针对性改写——去循环携带依赖、指针加`__restrict`、补齐内存对齐（alignas/posix_memalign）、内层循环访问连续、循环次数编译期可知加`#pragma GCC unroll`。

```bash
optcxx -O3 -march=armv8.2-a -fopt-info-vec-missed loop.cpp
objdump -d a.out | grep -cE '\s(vld|fmla|mla)\s'   # NEON指令计数
```

- 预期收益：热点循环4~8倍（128bit NEON对float4路）。

### 3.7 循环不变量外提与强度削弱

- 使用场景：循环内存在每次迭代重复计算的乘法、除法、寻址表达式。
- 使能方法：乘法换移位/加法、除以常量换乘逆+移位、不变表达式提到循环外；手写前先确认-O3是否已做（gcc通常能做简单情形），复杂表达式需人工。

```cpp
for (i...) y[i] = x[i] / 8.0;          // -> 乘 0.125 或移位（整数）
int idx = base + stride * i;           // 循环内 -> 改为 idx += stride 递推
```

- 预期收益：算术密集热点 -10%~30%。

### 3.8 函数内联与调用图优化

- 使用场景：热点路径小函数调用频繁，参数传递/压栈开销+阻断向量化。
- 使能方法：小函数让编译器自动内联（-O2/-O3）；跨编译单元用`-flto`或`inline __attribute__((always_inline))`；单次调用函数直接展开减参数处理开销（毕昇编译器Code Size优化思路反向应用）；热路径避免虚函数调用，用CRTP/模板静态分发或if-branch替换。

```cpp
inline __attribute__((always_inline)) int hot_small(int x) { return x + 1; }
// 虚调用热点: if (type==A) a->run(x); else b->run(x);  // 分支替换虚表
```

- 预期收益：小函数密集热点 -5%~20%。

### 3.9 LTO链接期优化

- 使用场景：多编译单元工程，跨文件调用无法内联、全局信息不可见。
- 使能方法：编译+链接均加`-flto`（毕昇/GCC9+支持），允许链接期内联、死代码消除、过程间常量传播；配合PGO（3.2）效果更佳。

```bash
optcxx -O3 -flto -c a.cpp b.cpp && optcxx -O3 -flto a.o b.o -o server
```

- 预期收益：整体 +2%~8%（跨文件调用多的工程收益更大）。

### 3.10 代码布局优化（-freorder-functions/haifa）

- 使用场景：icache/dcache miss高（perf前端周期front-end bound高）。
- 使能方法：`-freorder-functions`把热点函数聚到一起（配合PGO用profile精确布局）；函数对齐`-falign-functions=64`；热路径代码与冷路径（异常处理、日志分支）分离，冷代码挪出热cacheline。

```bash
optcxx -O3 -flto -freorder-functions -falign-functions=64 ...
# 前端瓶颈确认: perf stat -e cycles,instructions,idle-instructions front-end差距
```

- 预期收益：前端bound热点 -10%~25%。

## 4、OS层（NUMA是鲲鹏重中之重）

### 4.1 NUMA绑核/关平衡

- 使用场景：多NUMA服务器，跨node访问延迟1.5~2倍。
- 使能方法：

```bash
numactl --cpunodebind=0 --membind=0 ./server    # 进程绑node
taskset -pc 0-47 <pid>                          # 线程绑核
echo 0 > /proc/sys/kernel/numa_balancing        # 关自动页迁移
numastat -p $(pidof server)                     # remote占比高=有问题
# 容器场景: docker run --cpuset-cpus=0-47 --cpuset-mems=0
```

- 预期收益：RT -10%~30%，尾延迟显著改善。

### 4.2 大页

- 使用场景：特征库/索引大块常驻内存，TLB miss高。
- 使能方法：

```bash
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
sysctl vm.nr_hugepages=1024                    # 超大块用hugetlbfs
mount -t hugetlbfs nodev /mnt/huge             # 应用mmap(MAP_HUGETLB)映射
getconf PAGE_SIZE                              # 确认64K page开启
```

- 预期收益：TLB miss相关热点 -10%~40%。

### 4.3 内核隔离与中断亲和

- 使用场景：高QPS服务受时钟中断/软中断干扰，P99抖动。
- 使能方法：

```bash
# 内核启动参数: nohz_full=48-95 isolcpus=48-95 rcu_nocbs=48-95  (假设48-95为业务核)
systemctl stop irqbalance
for irq in $(cat /proc/interrupts | grep eth0 | awk '{print $1}' | tr -d ':'); do
    echo 2 > /proc/irq/$irq/smp_affinity            # 网卡中断绑到非业务核
done
sysctl vm.swappiness=0
```

- 预期收益：P99 -10%~30%。

### 4.4 网卡Offload与多队列

- 使用场景：perf中内核do_csum（TCP校验和）cycles>2%、__crc32c_le>8%，或全部网卡中断集中在单核。
- 使能方法：开启网卡校验和/GSO/TSO offload，让协议栈计算下沉网卡；多队列RSS把中断均匀散到各核，再绑到同NUMA（见4.3）。

```bash
ethtool -k eth0 | grep -E "rx-checksumming|tx-checksumming|tcp-segmentation"
ethtool -K eth0 rx on tx on tso on gso on gro on
ethtool -L eth0 combined 8                       # 多队列数=绑给本服务的核数
```

- 预期收益：内核协议栈CPU开销大幅下降，中断不集中单核。

### 4.5 异步批量IO

- 使用场景：文件读写（样本落盘、模型加载、日志）每秒>500次阻塞调用。
- 使能方法：libaio/io_uring批量异步提交（见1.10）；顺序写合并为大块；日志缓冲后批量刷盘。

- 预期收益：IO吞吐提升且不阻塞会话线程。


### 4.6 网络sysctl调优

- 使用场景：高QPS下SYN溢出、TIME_WAIT堆积、包丢弃（netstat -s看overflow/dropped）。
- 使能方法：

```bash
# /etc/sysctl.d/99-net.conf
net.core.somaxconn = 32768                # listen backlog上限
net.core.netdev_max_backlog = 32768       # 网卡收包队列
net.ipv4.tcp_max_syn_backlog = 16384
net.ipv4.tcp_tw_reuse = 1                 # TIME_WAIT快速复用
net.ipv4.tcp_fin_timeout = 15
net.ipv4.ip_local_port_range = 1024 65000 # 出连接端口池
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
# 应用listen()的backlog参数也要同步加大(内核取二者较小值)
```

- 预期收益：消除连接建立丢包与TIME_WAIT耗尽，QPS上限抬高。

### 4.7 网卡ring buffer与中断合并

- 使用场景：软中断NET_RX占比高、丢包（/proc/net/softnet_stat第三列非零）、小包洪峰延迟抖动。
- 使能方法：加大ring buffer防丢包；中断合并coalesce减少中断次数（延迟敏感场景慎用过大值）；softirq预算加大。

```bash
ethtool -G eth0 rx 4096 tx 4096                     # ring buffer拉到最大
ethtool -C eth0 rx-usecs 50 tx-usecs 50             # 中断合并微调
sysctl -w net.core.netdev_budget=600                # 单次softirq处理配额
watch -n1 'grep -E "NET_RX" /proc/softirqs'         # 观察软中断分布
```

- 预期收益：洪峰丢包消除，小包吞吐提升。

### 4.8 磁盘IO调度器与文件系统

- 使用场景：NVMe/SSD上默认CFQ类调度器多余、大量小文件元数据开销、样本落盘写放大。
- 使能方法：SSD/NVMe调度器设none或mq-deadline；文件系统高并发选XFS；挂载noatime；预读按业务调（顺序读大文件加大readahead，随机读减小）。

```bash
cat /sys/block/nvme0n1/queue/scheduler
echo none > /sys/block/nvme0n1/queue/scheduler
mkfs.xfs -f /dev/nvme0n1 && mount -o noatime,nodiratime /dev/nvme0n1 /data
blockdev --setra 4096 /dev/nvme0n1                  # readahead(512B扇区单位)
fio --name=t --direct=1 --iodepth=64 --rw=randread --ioengine=libaio --bs=4k --runtime=60 --filename=/dev/nvme0n1   # 基线测试
```

- 预期收益：NVMe随机IO延迟 -10%~30%，元数据CPU开销下降。

### 4.9 透明大页策略与numad

- 使用场景：内存访问模式动态变化、THP defrag造成延迟尖刺（P99偶发毫秒级抖动）。
- 使能方法：延迟敏感服务THP设madvise+defrag=madvise（避免khugepaged全局扫描停顿）；或numad自动绑核+内存本地化；确认64K页（鲲鹏推荐，见4.2）。

```bash
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
echo madvise > /sys/kernel/mm/transparent_hugepage/defrag
systemctl start numad                               # 自动NUMA本地化守护
```

- 预期收益：消除THP相关毫秒级延迟尖刺。

### 4.10 cgroup/容器资源隔离

- 使用场景：混部或容器部署下，离线任务/邻居容器抢占CPU与LLC。
- 使能方法：业务容器绑核+绑node（cpuset）；离线限权重；LLC/内存带宽用CAT/MBA隔离（需内核与硬件支持）；oom_score保护核心服务。

```bash
docker run --cpuset-cpus=0-47 --cpuset-mems=0 ...   # 业务容器
docker run --cpu-shares=2 ...                       # 离线容器限权重
# 宿主机RDT: resctrl挂载后配置CAT schemata按CLOS划分LLC
```

- 预期收益：混部下业务P99稳定，不被邻居拖垮。

## 5、芯片/BIOS/环境层

### 5.1 BIOS基线

- 使用场景：新机部署基线检查，必查项。
- 使能方法：

```bash
dmidecode -t memory | grep -E "Size|Speed|Locator"   # 每通道DIMM插满、频率拉满
# 少插一根内存条带宽几乎减半，软件无法补偿！
ipmitool raw 0x30 0x60 0x01                          # 确认Power Profile=Performance
lscpu | grep NUMA                                    # 与numactl -H交叉确认
```

- 预期收益：内存带宽可达2倍差距。

### 5.2 PCIe同NUMA侧

- 使用场景：网卡/SSD挂在与业务不同node的PCIe上。
- 使能方法：

```bash
cat /sys/class/net/eth0/device/numa_node             # 网卡归属node
cat /sys/block/nvme0n1/device/numa_node              # SSD归属node
# 与业务进程node不一致 => 迁网卡插槽或业务改绑该node
```

- 预期收益：跨node IO延迟 -30%~50%。

### 5.3 CPU频率性能模式

- 使用场景：默认ondemand/降频。
- 使能方法：

```bash
cpupower frequency-set -g performance
cpupower frequency-info                              # 确认实际频率无温控降频
```

- 预期收益：整体 +10%~30%。

### 5.4 KAE硬件加速引擎

- 使用场景：SSL/TLS加解密（RPC/WBE网关握手）、数据压缩（gzip/zstd/snappy）占用大量CPU；鲲鹏920内置硬件加速单元可卸载。
- 使能方法：装KAE驱动+库，兼容OpenSSL/Zlib/ZSTD/LZ4/Snappy标准接口，业务代码零修改。规格：AES 60Gbit/s、SM4 30Gbit/s、RSA2048异步签名54384 sign/s（软件735，提升74倍）；包>256字节建议走硬件。

```bash
# 安装（openEuler可直接yum）
yum install uadk                # 用户态加速框架
# 加解密走engine; 压缩用KAEZlib/KAEZstd/KAELz4/KAESnappy替换对应库
openssl speed -elapsed -engine kae -async_jobs 36 rsa2048   # 验证加速生效
# 提升instance上限(默认256/加速器,最大1024): modprobe hisi_zip pf_q_num=1024
```

- 预期收益：加解密/压缩CPU占用大幅下降（官方：Nginx RSA加速提升35%；HBase硬件加密读写接近不加密性能）。

### 5.5 L3分区模式

- 使用场景：L3延迟敏感业务；鲲鹏920的L3支持BIOS设置private/partition/shared模式，shared模式下单核用满L3延迟>90周期。
- 使能方法：BIOS中把L3设为private/partition模式，近4MB内L3延迟约36周期（shared模式显著更差）；业务部署尽量不超单分区容量，超了按NUMA分片（见4.1）。

- 预期收益：L3访问延迟稳定在约36周期，跨分区访问避免。


### 5.6 BIOS内存配置项

- 使用场景：新机装机或性能不达预期时的BIOS逐项检查（鲲鹏920服务器）。
- 使能方法：

```text
Advanced > Memory Config:
  NUMA = Enable                     # 开NUMA，配合软件绑核
  Die Interleaving = Disable        # 关die交织，否则访存变慢
  Rank Interleaving = 4-way Interleave
  One NUMA Per Socket = Disabled    # 关闭以获得每CPU多NUMA粒度
  Custom Refresh Rate = Auto
```

- 预期收益：内存子系统达到设计带宽，配置错误可致访存明显变慢。

### 5.7 CPU预取开关

- 使用场景：HPC/打分类负载访问模式集中（顺序/步长固定），预取可有效隐藏内存延迟；访问随机（如大hash查表）时反而浪费带宽。
- 使能方法：BIOS "CPU Prefetching Configuration"开关按负载访问模式取舍——数据集中开、随机访问关；软件侧配合prefetchw/__builtin_prefetch指令精细控制。

```cpp
__builtin_prefetch(&items[i + 8], 0, 1);   // 提前取8个元素后的缓存行
```

- 预期收益：顺序负载内存延迟显著隐藏；误开随机负载则带宽浪费。

### 5.8 CPPC能效协作与调频模式

- 使用场景：Performance模式固定标称频率，但部分场景需要动态调频平衡功耗与突发；或发现频率没跑满。
- 使能方法：BIOS Power Policy=Efficiency时支持CPPC动态调频，内核调频策略用schedutil响应及时；延迟敏感固定Performance（见5.3）；检查无温控降频。

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
echo schedutil > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
turbostat --quiet --show Bzy_MHz --interval 1     # 观察实际频率
```

- 预期收益：突发负载响应更快或功耗优化，视场景二选一。

### 5.9 内存带宽基线与通道检查

- 使用场景：内存带宽型负载（打分GEMM、向量检索）达不到设计带宽，怀疑通道没插满。
- 使能方法：用stream测带宽基线与理论值对比；鲲鹏920每CPU 8通道，2P满配16通道，1DPC最高3200MHz；dmidecode逐条核对（见5.1）。

```bash
make -C stream/ && ./stream_c.exe    # Triad值与理论带宽比
# 2P鲲鹏920满配可达数百GB/s, 若只有一半 => 检查DIMM插槽数
```

- 预期收益：发现并修复"少插内存条带宽减半"类部署问题（软件无法补偿）。

### 5.10 NIC/加速器PCIe拓扑与AF_XDP

- 使用场景：高PPS收包场景内核协议栈成为瓶颈；或网卡/加速器跨NUMA。
- 使能方法：确认拓扑同node（见5.2）；极限收包用AF_XDP/XDP在驱动层处理或转发内核栈；accelerator（KAE等）确认挂在本node的PCIe root port。

```bash
lspci -vvv -s <bdf> | grep -E "Numa|LCRV"
# XDP示例: ip link set dev eth0 xdp obj xdp_prog.o section xdp
```

- 预期收益：收包路径绕过内核栈，PPS提升数倍；跨node访问消除。

## 6、通用手段

### 6.1 预热/降级/背压

- 使用场景：上线冷启动、超载保护。
- 使能方法：新实例先灌热缓存再接流量；下游超时降级、请求合并；过载背压防雪崩。目标P99稳定而非均值。

```bash
curl -s localhost:8080/warmup?size=full              # 启动后主动拉全量热数据
# 负载网关侧：下游P99超阈值自动降级（丢弃低价值特征路/走简化打分）
```

- 预期收益：尾延迟稳定性。

### 6.2 分级存储

- 使用场景：特征库超内存，全量放SSD导致长尾IO。
- 使能方法：热特征内存（监控命中率）、冷特征本地NVMe mmap，访问冷数据异步回填。

```cpp
class TieredFeatureStore {
    MemCache hot_;
    MmapStore  cold_;                                // /mnt/nvme/feat.bin
public:
    V Get(K k) {
        if (auto* v = hot_.Get(k)) return *v;
        auto v = cold_.Read(k);
        pool_->Submit([k, v] { hot_.Put(k, v); });   // 异步回填，不阻塞会话
        return v;
    }
};
```

- 预期收益：成本可控下保热数据性能。

### 6.3 Hyper Tuner自动分析

- 使用场景：接手服务无明确热点方向，或团队缺少调优经验，需要系统性体检。
- 使能方法：华为Kunpeng Hyper Tuner（免费），采集一段真实流量后自动给出优化建议清单：编译选项（CPU占用>50%时建议-O3/-mtune）、TCP checksum offload、CRC32汇编实现、malloc换jemalloc、锁竞争、select换epoll、NUMA切换过多等，附具体修改方法。

```bash
# WebUI采集->Overall Analysis->Optimization Suggestion Manual逐条落实
# 支持HPC/内存访问/IO/C&C++专项分析
```

- 预期收益：自动生成可执行的优化建议清单，覆盖本章大部分方法。

### 6.4 火焰图on/off-CPU分析

- 使用场景：P99尖刺但均值正常、常规监控全绿定位不出根因。
- 使能方法：先问"尖刺时CPU忙吗"——on-CPU火焰图（perf record -g）看CPU在忙什么（不该忙的：锁、malloc、拷贝），off-CPU分析看在等什么（IO、锁等待、调度延迟）；两类根因互补覆盖全部P99根因谱系。

```bash
# on-CPU
perf record -F 99 -g -p <pid> -- sleep 30 && perf script | stackcollapse | flamegraph.pl > oncpu.svg
# off-CPU (需要offcputime或eBPF)
/usr/share/bcc/tools/offcputime -p <pid> 30 > offcpu.txt
```

- 预期收益：P99根因二选一定位到具体调用栈。

### 6.5 容量与限流基线

- 使用场景：优化后不确定能扛多少QPS、大促前无水位依据、过载即雪崩。
- 使能方法：每轮优化后压测定容量基线（单实例QPS上限、P99拐点）；入口按基线配限流（令牌桶/并发数）；下游按P99配超时与重试预算（重试次数×超时≤上游预算）。

```bash
# 压测找P99拐点: wrk2固定速率阶梯加压
wrk -t8 -c64 -R2000 --latency -d30s http://host:port/predict   # 每轮提500RPS
# 限流: brpc内置 AutoConcurrencyLimiter / 网关侧令牌桶
```

- 预期收益：过载不雪崩，优化收益转化为可承诺的容量数字。














### 6.6 Tuned自动配置档

- 使用场景：不想逐项手调OS参数，需要标准化的整机性能profile。
- 使能方法：tuned-adm套用throughput-performance/network-latency等预置档（自动配调度器、sysctl、能耗参数），再在档上叠加业务特调。

```bash
tuned-adm profile network-latency      # 低延迟档
tuned-adm active                       # 查看当前档
```

- 预期收益：一条命令获得调优基线，避免漏配。

### 6.7 中断与软中断监控

- 使用场景：P99抖动但CPU整体不高，怀疑软中断/单核热点。
- 使能方法：监控/proc/interrupts与/proc/softirqs分布，单核NET_RX集中即绑核问题；mpstat看%soft列。

```bash
watch -n1 'grep -E "CPU|NET_RX|NET_TX" /proc/softirqs'
mpstat -P ALL 1                                # %soft列
```

- 预期收益：快速定位中断集中的核，指导绑核/offload决策。

### 6.8 压测流量回放与影子验证

- 使用场景：优化效果需真实流量验证、大促前压测但不敢打真实流量。
- 使能方法：录制线上请求（brpc rpc_replay/tcpcopy）回放到影子集群；对比优化前后同流量下P50/P99/QPS；金丝雀灰度小流量验证（配合1.19）。

```bash
# brpc: rpc_replay回放录制的流量
rpc_replay -port 8000 -replay_file trace.out
```

- 预期收益：优化收益有真实数据背书，上线风险可控。

### 6.9 性能回归看板与告警

- 使用场景：优化成果随代码迭代回退，无人察觉。
- 使能方法：核心接口RT/QPS分位数入库+看板（Prometheus+Grafana）；发布后自动对比基线版本；P99回退超阈值阻断发布。

- 预期收益：性能与正确性同级纳入CI门禁，防"优化成果蒸发"。

### 6.10 效果归因方法论

- 使用场景：多项优化同时上线，说不清谁贡献了多少，后续无法决策保留/回退。
- 使能方法：一次只改一个变量；每项优化前先测基线、后测结果、记录环境（频率/绑核/数据量）；优化项之间做A、B、A+B小矩阵验证组合效应非线性；所有实验数据归档可复跑。

- 预期收益：每项收益有据可查，团队形成实验文化。

## 使用原则

1. 先用perf定位热点，按总表匹配方法，禁止无数据盲目套用。
2. 每项优化A/B验证：同压测下交替运行取中位数（连续单跑同机波动可达±40%），对比QPS/RT(P99)+归因。
3. 一次只改一个变量。
4. 结构性收益大于微调：执行路径重构(1.11)、过滤前置、旁路解耦这类"少做事"的优化，通常大于单点加速"把事做快"。

## 参考来源

- 博客园《粗排治理之性能优化》：https://www.cnblogs.com/xianzhedeyu/p/17109544.html（方法1.11/1.12/1.4补充/1.8补充实测来源）
- 美团技术团队《美团搜索粗排优化的探索与实践》：https://tech.meituan.com/2022/08/11/Coarse-Ranking-Exploration-Practice.html（方法1.13/NAS/AutoFAS）
- 得物技术《粗排优化探讨》：https://developer.volcengine.com/articles/7322130383351316489（方法1.13/COLD/PFD）
- 博客园《大规模向量检索与量化方法》：https://www.cnblogs.com/zackstang/p/18553945（方法1.14，OpenSearch十亿级实测数据）
- brpc官方性能文档：https://brpc.apache.org/docs/rpc-in-depth/io（方法1.15）
- Google《The Tail at Scale》解读：https://juejin.cn/post/6960723961747341343（方法1.16）
- AWS/Feast/Redis特征存储实践：https://aws.amazon.com/blogs/database/build-an-ultra-low-latency-online-feature-store-for-real-time-inferencing-using-amazon-elasticache-for-redis/（方法1.17）
- 鲲鹏Hyper Tuner优化建议手册：https://support.huawei.com/enterprise/en/doc/EDOC1100178021（方法1.9/1.10/2.4/3.3/4.4/5.4触发条件与修改方法）
- KAE鲲鹏加速引擎：https://github.com/kunpengcompute/KAE（方法5.4，含算法规格与实测）
- openEuler KAE使用指南：https://docs.openeuler.org/en/docs/20.09/docs/Administration/using-the-kae.html（方法5.4实测数据）
- NVIDIA HugeCTR HPS推理参数服务器：https://arxiv.org/pdf/2210.08804（方法1.15，hot cache与层级存储实测）
- 腾讯云《推荐算法架构——重排》：https://cloud.tencent.com/developer/article/1976753（方法1.19/1.20/1.21，MMR/DPP/规则引擎）
- 迪吉老农《MMR+DPP》：https://yandi.space/study/dpp/%E9%87%8D%E6%8E%92%E6%89%93%E6%95%A3%E8%AE%BA%E6%96%87/（方法1.19，滑窗与SSD正交化优化）
- NVIDIA TensorRT最佳实践/昇腾CANN atc：https://developer.nvidia.cn/blog/tensorrt-measuring-performance-cn/（方法1.18，量化与融合）
- 鲲鹏性能优化十板斧（CPU内存/网络/磁盘IO）：https://www.hikunpeng.com/document/detail/zh/perftuning/tuningtip/kunpengtuning_12_0005.html（方法4.6~4.10/5.6~5.9）
- 鲲鹏HPC服务器BIOS配置建议：https://www.hikunpeng.com/doc_center/source/zh/kunpenghpcs/hpcindapp/tngg/kunpenghpcsolution_05_0007.html（方法5.6/5.7）
- 鲲鹏编程与调优指南：https://www.hikunpeng.com/doc_center/source/zh/perftuning/progtuneg/（方法3.6~3.10）
- chipsandcheese鲲鹏920架构分析：https://chipsandcheese.com/p/huaweis-kunpeng-920-and-taishan-v110（方法3.5/5.5 L3实测）
