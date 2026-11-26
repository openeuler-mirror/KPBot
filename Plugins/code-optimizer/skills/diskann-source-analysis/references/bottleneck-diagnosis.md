# DiskANN 瓶颈诊断

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 要求提供基线 Manifest 和原始 profile 目录；任一缺失时分别使用 diskann-benchmark-baseline 补齐基线或采样。
2. 读取 [diagnosis-tree.md](diagnosis-tree.md)。
3. 将耗时归一化到每查询；必要时进一步计算每 I/O、每 comparison、每 hop 成本。
4. 将墙钟时间拆为 CPU-active、I/O-wait/off-CPU、调度/锁和未解释部分。
5. 将热点关联到源码阶段：PQ 表构建、候选队列、visited 检查、frontier 选择、cache 查询、磁盘读取、已加载节点处理和全精度 refine。
6. 输出排序后的假设，逐项写明证据级别、最大可能收益、验证实验、风险和证伪信号。
7. 将候选交给 diskann-optimize，并按需读取其 I/O、SIMD 或内存/布局参考资料；混合机制在分别测量前不得合并实施。

## 必需输出

| 排名 | 假设 | 证据 | 预计上限 | 下一实验 | 证伪条件 |
| --- | --- | --- | --- | --- | --- |

明确写出数据限制。除非能说明每查询工作量或单位成本发生了什么变化，否则不得把“数据量大”或“维度高”直接写成原因。

## 按需资源

- [diagnosis-tree.md](diagnosis-tree.md)：从 CPU、I/O、内存、调度和工作量证据建立归因树时读取。
