---
name: faiss-simd-optimization
description: 根据采样、内核分派和数值约束评估 Faiss AArch64 的 NEON/SVE/SVE2 优化。适用于距离、查表、解码或归约内核的 SIMD 选型、尾部与回退验证。
---

# Faiss ARM SIMD 优化

## 输入

热点符号、实际内核与编译 flags、目标 ISA/向量长度、维度和布局、数值与正确性要求。

## 执行步骤

1. 以分派、反汇编和采样确认执行路径，区分已有自动向量化与未向量化部分。
2. 分析依赖链、gather/查表、归约、对齐和寄存器占用，选择 NEON、SVE 或保留当前实现。
3. 设计局部 guard 与通用回退；按目标编译器/ISA 的可靠资料核对 intrinsic、lane 和特性依赖。
4. 验证短输入、非整向量长度、非对齐与异常浮点输入；用实际汇编和冻结对照检查最终效果。

## 输出

选型依据、受保护候选、正确性用例、编译/汇编证据及性能结论。

## 约束

更宽向量、较少源码语句或消除调用都不能单独证明收益。浮点归约顺序、FMA、溢出和尾段语义必须符合任务契约。

## 参考资料

- [内核导航](references/arm-simd-source-map.md)
- [NEON/SVE 决策](references/neon-sve-decision-guide.md)
- [常见改写与风险](references/simd-optimization-patterns.md)
- [实际启用证据](references/simd-evidence.md)
