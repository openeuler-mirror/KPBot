---
name: faiss-ivfpqfs-optimization
description: 分析和优化 Faiss IndexIVFPQFastScan 的打包码字、量化 LUT、块扫描和结果处理。适用于 bbs/qbs/implem 解释、SIMD 查表热点定位及候选设计；普通 IVFPQ 使用 faiss-ivfpq-optimization。
---

# Faiss IVFPQ FastScan 优化

## 输入

FastScan 源码与构建信息、M/nbits、bbs、qbs、implementation selector、查询批量、metric 和结果处理方式。
源码、构建选项和运行时行为以目标检出为准；版本号本身不能证明某个扩展内核或后端存在。

## 执行步骤

1. 确认索引类型、packed code 布局及实际 implementation；自动分派结果与配置请求值分别记录。
2. 追踪 float LUT、整数表量化、LUT 打包、块累加和 result handler，核对偏置、缩放与整数累加范围。
3. 结合实际 query 批量和采样定位 setup、查表、handler 或可选精排的成本，评估摊销条件后选择候选。
4. 保持块布局、码字含义、候选顺序和数值契约；覆盖非整块、量化边界、heap/reservoir 与通用回退。

## 输出

源码与布局说明、带采样依据的热点归因、候选机制及适用条件、正确性/性能验证计划。完整实验由 `faiss-optimize` 编排；本技能提供算法专属判断。

## 约束

- SIMD 方案根据实际 ISA、向量长度、查表代价和寄存器压力验证。改变 bbs/qbs/implem、查询批量或量化策略应建立独立对照。
- 单个候选使用默认关闭的特性开关，保留通用回退；性能结论须来自冻结条件下的实际对照，静态汇编变化只证明实现形态。
- 已有实验仅在源码、配置和平台条件相关时作为参考；无记录时依据当前证据继续。

## 参考资料

- 调用链：[源码入口](references/ivfpqfs-algorithm-and-source.md)
- 参数、布局或机制：[布局与适用条件](references/ivfpqfs-layout-and-fastscan.md)
- 测试映射：[验证与测试配置](references/ivfpqfs-sra-test-profile.md)
- 采样归因：[热点检查项](references/ivfpqfs-performance-hotspots.md)
- 候选设计：[候选与验证](references/ivfpqfs-optimization-candidates.md)
- 冻结字段：[benchmark-contract.json](references/benchmark-contract.json)

公共构建、源码、采样、SIMD 和测试方法分别使用 `faiss-build`、`faiss-source-analysis`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test` 和 `vector-ut-st-validation`。
