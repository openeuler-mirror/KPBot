# 阶段 4：RaBitQ/HGraph 计算优化

先用 profiling 或阶段计时区分 ROM 变换、query 后处理、图遍历、距离 lookup/L2 恢复、visited tracking 和 heap 成本，再选择优化，不凭静态猜测堆叠改动。

## 优化方向

1. 将多 query 的稠密随机正交变换合并为 CBLAS/OpenBLAS row-major SGEMM。
2. 优化转置 4-bit lookup 的 SIMD/SVE 实现，并融合 L2 recovery；L2 与 IP/Cosine 必须在 quantizer 容差内匹配标量 `ComputeDist`。
3. 对连续候选 code 使用快路径，但必须验证真实 pointer stride，不能只假设逻辑 ID 连续。
4. 减少 gathered scratch code 的复制和临时分配；只有 allocator ownership 兼容时才复用 scratch buffer。
5. 改善 HGraph visited locality、候选/结果 heap 的分支和对象访问。
6. 仅在目标机器范围有实测证据时保留 prefetch distance 或机器特化。

## 优化纪律

- 一次实现一个可归因的优化点，保留阶段前后的独立数据。
- 小 batch 可能更适合直接向量路径而不是 SGEMM，应允许自适应切换。
- 不得破坏 singleton、无 SVE、generic quantizer 或功能关闭路径。
- 不改变索引布局、距离定义、召回率判据和浮点容差。

阶段 4 可以和阶段 3 分别迭代；任何变更都必须重新通过阶段 5。


