# Visited 结构与物理布局

## HashSet 与 bitmap

设 N 为数据库向量数，V 为每查询访问节点数。

- Bitmap payload：每查询 `ceil(N/8)` 字节，另加清零策略成本。
- HashSet payload：取决于 capacity、load factor、control bytes、key 大小、allocator 和保留容量；它随 V 而不是 N 增长。

历史估算采用 u32 key、每 bucket 约 1 个 control byte、87.5% load factor：

| 数据集 | Visited 比例 | HashSet 估算 | Bitmap | Bitmap / HashSet |
| --- | ---: | ---: | ---: | ---: |
| OpenAI-2M | 0.526% | 0.313 MiB | 0.277 MiB | 0.89× |
| BigANN-10M | 0.090% | 0.156 MiB | 1.192 MiB | 7.63× |
| Deep-10M | 0.322% | 0.625 MiB | 1.192 MiB | 1.91× |

bitmap 仍可能在查询局部性上获益，但物理清零和每 worker 副本可能成为主要成本。可以评估 generation tag、稀疏分块 bitmap 或可复用 buffer，并进行端到端测量。

## Vertex ID 到磁盘位置

在已研究的 Rust 构建路径中，输入行枚举值成为 vertex ID，磁盘序列化按 ID 进行。因此除非输入本身已排序，否则图上相近或向量相近的节点不保证物理相近。必须在当前 commit 重新核对文件行号证据。

重排需要同步更新：

- vertex ID 与邻接 neighbor ID；
- medoid/entry point；
- PQ code 行关联；
- 全精度向量/associated data；
- label、filter、ground truth 映射和 external-ID translation。

## Sector、extent 与 LBA

- 索引格式中的 sector/block 是逻辑对齐读取单元。
- extent 是文件系统把连续文件区间映射到连续物理块的记录。
- LBA 标识块设备位置。

相邻逻辑 sector 可能位于同一或不同 extent。减少 extent 可能降低映射或类似 seek 的开销，但不会让无关 vertex ID 获得语义局部性。
