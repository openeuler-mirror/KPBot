# 阶段 6：性能测试与调优

仅在阶段 5 通过后执行。每个优化点都与同配置干净基线比较，使用中位数或明确声明的聚合方法。

## 基准矩阵

默认按用户要求绑定 16 个连续物理核心，并记录准确 core set 与 NUMA placement。扫描至少包括：

- search threads：16、32、64、96，或用户指定范围；
- `OPENBLAS_NUM_THREADS` / `OMP_NUM_THREADS`：从 1 开始，避免默认过度订阅；
- `VSAG_QUERY_BATCH_MAX_SIZE`；
- `VSAG_QUERY_BATCH_TIMEOUT_US`；
- 用户要求的数据集、维度、metric、top-k、`ef_search`、recall 和 batching flag。

“16 cores”不等于“16 search threads”。报告同时给出 QPS、latency、recall、observed batch size、warmup、重复次数和聚合方法。

## 调优与归因

- 协调 HGraph caller concurrency 与 BLAS 线程；更多 BLAS 线程可能因小矩阵同步和 traversal 竞争降低 QPS。
- 分离 query gather、ROM SGEMM、postprocess、HGraph traversal、lookup/L2、visited 和 heap 的耗时。
- 对负收益先复测，再判断测试噪声、batch 等待、拷贝/分配、线程竞争、带宽、prefetch 或 SVE 行为。
- 跨机器退化依次检查 commit/index/config、affinity/NUMA、BLAS threads、observed batch/timeout、阶段计时、bandwidth/prefetch/SVE。
- 高维和低维可以采用不同 batch 策略；优先自适应策略而非机器常量。

历史数据只能作为来源示例，不能作为验收阈值。未验证的平台不得外推性能结论。


