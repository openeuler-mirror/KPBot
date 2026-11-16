# I/O 路径与候选检查表

## 端到端路径

1. worker 在 DRAM 中创建 query scratch 和 PQ lookup table。
2. candidate queue 选择 frontier 节点。
3. 静态 cache 命中时跳过磁盘访问；miss 时解析节点对应的磁盘 sector。
4. worker 准备对齐的目标 slots 和 io_uring SQE。
5. submit 进入内核；设备通过 DMA 将数据写入指定内存。
6. CQE 标识已经完成的请求。
7. 解析已加载节点，计算全精度/PQ 距离并合并候选。
8. 重复上述过程，直到满足终止条件。

必须在当前源码中逐步验证；不同实现的名称和 batch 方式可能不同。

## 指标

- 应用：submit 调用、SQE、CQE、reads/query、bytes/query、wait/drain 时间；
- 有效性：节点字节与对齐读取字节、实际消费的 neighbor IDs、cache hit rate；
- 设备：await、queue depth、throughput、IOPS、tail latency；
- 重叠：首个到最后一个 CQE 的时间窗，以及窗口内完成的 CPU 工作；
- 布局：same-sector/adjacent-sector 比例、extent 数、LBA 差值。

## 预取决策

只有未来消费概率和设备/OS 效率足以抵消额外字节、队列 slot、cache pollution 和 latency 时，相邻 sector 预取才可能获益。扩大读取尺寸前必须测量相邻性和有效利用率。
