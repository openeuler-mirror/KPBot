# DiskANN SIMD 优化

文中命令相对所属 skill 根目录执行。

## 执行流程

1. 在真实 benchmark 中确认实例化的数据类型、距离类型、PQ bit 宽度和调用点时间占比。
2. 读取 [simd-evidence.md](simd-evidence.md) 和 [pq-lookup.md](pq-lookup.md)。
3. 记录编译器、目标 flags、二进制 hash、符号地址和当前反汇编。
4. 获取真实维度、对齐、chunk 数和 batch 大小后，再构造代表性微基准。
5. 一次实现一个 NEON/SVE/SVE2 候选，保留 feature gate 和 scalar/reference 路径。
6. 覆盖边界长度、tail、不同对齐和数据类型，按精确一致或预先声明的容差校验输出。
7. 重新运行冻结的端到端数据集；只有微基准改善的候选不得接受。

## 约束

- 源码出现 intrinsic 不代表当前二进制实际使用了该指令。
- SVE 向量长度由运行环境决定；除非明确只面向一台机器，否则不得写死 256-bit 语义。
- 4-bit packed code 必须正确处理高低 nibble 和 LUT 索引。
- 预取距离依赖负载和 cache，应作为独立候选测量。

## 按需资源

- [simd-evidence.md](simd-evidence.md)：检查编译目标、反汇编、perf 与端到端证据链时读取。
- [pq-lookup.md](pq-lookup.md)：分析 4-bit/8-bit PQ 打包、LUT 索引和向量化约束时读取。
