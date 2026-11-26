# AArch64 SIMD 证据协议

1. 定位源码符号和 cfg/feature 选择。
2. 保存准确构建命令及 `-C target-feature`/`-march`/`-mcpu` 设置。
3. 使用 `nm`、`objdump -d`、`cargo asm` 或项目可用工具映射热点符号。
4. 确认采样 PC 落在预期实现中。
5. 对指令分类：scalar、Advanced SIMD/NEON、dot-product、SVE、SVE2、load/store/prefetch。
6. 比较 retired work、cycles、IPC、cache 行为和 wall time。

有效指令族取决于实际实现，例如 vector load、widening/narrowing、table lookup、horizontal reduction、`sdot`/`udot`、SVE predicate loop 和 `prfm`。没有上述二进制证据时，不得将它们列为“当前正在使用”。

一个历史本地构建的微基准约为：128D squared L2 20 ns、128D inner product 16 ns、100D cosine 32 ns。这些值只能作为比较线索；复用前必须找回二进制和测试 harness。
