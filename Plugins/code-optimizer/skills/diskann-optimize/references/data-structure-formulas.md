# DiskANN 数据结构公式

## PQ pivots

设原始维度为 D、chunk 数为 C、每 chunk centroid 数为 K=256、f32 元素大小为 4 B。平均 chunk 维度为 D/C。

`PQ pivots payload = C × K × (D/C) × 4 B = D × 1024 B`

该公式不包含文件 header、offset、centroid metadata、对齐和重复副本。

## PQ codes

- 8-bit code: approximately `N × C` bytes.
- 4-bit packed code: approximately `N × ceil(C/2)` bytes.

必须核实 padding 和实际编码格式。

## Per-query LUT

- 8-bit/K=256: `C × 256 × 4 B`.
- 4-bit/K=16: `C × 16 × 4 B` when a 16-entry LUT is used.

总 scratch 应乘以并发 query/worker 数，而不是数据库向量总数。

## 磁盘节点记录

常见 payload 下界为：

`vector_bytes + 4 B neighbor_count + 4 B × stored_neighbor_slots + associated_data`

序列化后的 `node_len` 可能包含固定最大度 slots、padding 和 alignment。只有格式允许一个 sector 存放多个固定记录时，节点数才能使用 `floor(sector_size / node_len)`；必须核实 header 和 sector 规则。

## 单位

- 十进制：kB/MB/GB/TB 使用 1000 的幂。
- 二进制：KiB/MiB/GiB/TiB 使用 1024 的幂。

大型结构必须同时报告字节数和无歧义的换算单位。
