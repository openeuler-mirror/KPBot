# DiskANN 瓶颈诊断树

## 1. 先检查逻辑工作量

逐查询比较：

- L 与 beam width；
- IOs、Comps、Hops；
- cache hit/miss 与加载节点数；
- 向量维度/数据类型和全精度距离计算次数；
- PQ chunks 与查表次数；
- query 数与召回目标。

较小数据集的 L 和每查询 IOs 显著更高时，它也可能比大数据集查询慢。图搜索不扫描全部 N 个向量，数据集基数不能单独决定查询时间。

## 2. 拆分成本

- CPU-active：queue/PQ/distance/copy/parse 函数中采样 cycles 较高。
- I/O wait：submit/wait/drain 周围存在应用计时或 off-CPU 证据，并由设备读取时延和队列深度佐证。
- Memory：低 IPC，同时出现 cache/TLB miss、带宽压力、NUMA 远端访问或大块 scratch 清零。
- 同步/调度：context switch、migration、lock/futex sample 或线程过量。

## 3. 候选收益上限

只有测量受影响时间占比后才能使用 Amdahl 定律。如果某函数只占 wall time 的 8%，即使完全消除也无法带来 10% 端到端收益。I/O 与 CPU 重叠时，应分析时间线，不能直接相加孤立阶段耗时。

## 4. 常见误区

- CPU utilization 同时包含有效工作和 spin/poll。
- SSD `%util=100` 仍可能对应低队列深度或小块随机读。
- `perf` sample 主要展示 on-CPU 时间；阻塞需要 off-CPU 或应用计时证据。
- 文件复制后 I/O latency 降低可能来自 extent/layout 或温度状态，不一定来自算法局部性。
- queue 微基准不能直接预测完整查询，因为真实规模下 libc 搬移和分支行为不同。
