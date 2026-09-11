# 阶段 4：线程与 blocking

## Family blocking

只建模线程填充率和 tail 的通用评分会把 M32 切成低效的 M4/M8。对有真实数据支持的 family 使用窄范围固定计划，未命中的 shape 回到通用搜索。SVE256、batch=1、M=32、N=256/512、K≥2048 的已验证计划为：compute N32、M32 不切分、N256/K256 或 N512/K512，`kThreads=min(4,max(1,floor(threads/nTiles)),kChunks)`。

每条 family 规则都要断言 n/m/k block 与 nthr_k；条件不要泛化成未经测量的 `M<=64`。

## Parallel-K

空间 work 远少于线程且 K chunks>1 时，优先沿 K 并行而不是切小 M。每个 K worker 写独立 partial product，由每个空间 work 的 leader 做 SVE reduction，再执行一次 `BS=0 + skipAccumulation` epilogue。

compute、barrier、reduce 放在同一 OpenMP region，避免创建两次 team；但 `kThreads==1` 必须走无 barrier 路径。scratch 尺寸使用安全整数运算，并由 pool 管理。

## 线程协调与 NUMA

执行期只启动 `min(spatial_threads, spatial_work) × k_threads`。检测到外层并行时退化为单线程，避免嵌套 team。单线程可使用更大的 PackB 复用组，多线程要保留空间切分。

构造时的满线程 blocking 不可直接用于多实例或运行时线程数骤降；若只 clamp kThreads 而不重搜 mBlock，必须记录为已知局限。单 node 固定核测量，跨 node 结果不直接比较。

## kBlock 与预取

对 `256/512/768/1024` 做实测扫描，观察循环摊销和 K tail 的交点。B 预取应覆盖下一个完整 reduction block 的多条 cache line；A 按有效行和 cache-line 消费节奏预取。所有预取改动使用 JIT dump/反汇编确认 `prfm` 的地址模式，不以“生成了指令”代替验证。
