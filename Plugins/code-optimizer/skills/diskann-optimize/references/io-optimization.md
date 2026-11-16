# DiskANN I/O 优化

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 在当前 commit 上追踪 frontier 选择和 cache miss，直至 offset 计算、SQE 创建、提交、完成和已加载节点处理。
2. 读取 [io-path-and-candidates.md](io-path-and-candidates.md)；设计对照时按需读取 [known-experiments.md](known-experiments.md)。
3. 量化每查询读取数、每次读取字节、节点有效字节、队列深度、提交批次、等待时间、完成事件离散度、设备时延，以及可用于重叠的 CPU 工作。
4. 按实测收益上限和语义风险排序候选。
5. 一次只实现一个候选，并提供 feature/runtime gate；保持逻辑工作量一致。
6. 先完成实现者自检。仅当用户明确提出代码审查时执行 CP4 diff review；否则记录 `CP4_SKIPPED_NOT_REQUESTED`，直接进入构建、冒烟和冻结实验。

## 候选类别

- 提交/完成：批量大小、非阻塞 submit、增量 drain CQE、完成事件提前派发。
- 缓冲区路径：registered buffer、fixed read、slot 复用、对齐和零拷贝解析。
- 工作重叠：其他 I/O 仍在途时立即处理已完成节点。
- 逻辑布局：热点/cache 节点、BFS/共访问重排、相邻 sector 预取。
- 物理布局：extent、defrag/copy 对照、LBA 顺序、文件系统和设备行为。

## 硬门禁

- 对纯实现优化，Recall、IOs、Comps 和 Hops 保持一致。
- CQE 乱序完成时，user data 必须映射到正确请求和目标 slot。
- 正确处理错误、取消和部分完成路径。
- I/O 和消费者结束前不得复用相应 buffer。

## 按需资源

- [io-path-and-candidates.md](io-path-and-candidates.md)：分析当前 I/O 调用链、指标和候选类型时读取。
- [known-experiments.md](known-experiments.md)：查看已整理的代表性 I/O 实验契约时读取。
