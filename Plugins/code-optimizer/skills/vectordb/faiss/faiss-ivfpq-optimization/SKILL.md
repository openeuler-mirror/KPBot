---
name: faiss-ivfpq-optimization
description: 分析和优化 Faiss IndexIVFPQ 的倒排扫描、PQ 查表、候选选择与可选精排。适用于参数和布局解释、查询热点定位及等价实现候选设计；FastScan 使用 faiss-ivfpqfs-optimization。
---

# Faiss IVFPQ 优化

## 输入

`IndexIVFPQ` 源码与构建信息、metric、nlist/nprobe、M/nbits、残差与预计算表模式；若有精排，补充包装索引和候选倍率。
源码、构建选项和运行时行为以目标检出为准；版本号本身不能证明某个扩展内核或后端存在。

## 执行步骤

1. 从实际索引或测试程序追踪粗量化、每列表 LUT、码字扫描、候选堆和可选精排，确认 decoder 与内核分派。
2. 核对码字大小、子空间维度、表尺寸和读取方式；区分普通连续码字与 FastScan 的块打包布局。
3. 根据采样区分 LUT 建表、查表累加、heap 和精排开销，再选择固定形状特化、访存或循环组织等单一候选。
4. 保持候选顺序、距离累加与比较语义；验证形状 guard、通用路径、selector、metric 和尾段，再执行性能对照。

## 输出

源码与布局说明、带采样依据的热点归因、候选机制及适用条件、正确性/性能验证计划。完整实验由 `faiss-optimize` 编排；本技能提供算法专属判断。

## 约束

- nlist/nprobe/M/nbits/top-k、精排倍率和线程数属于实验配置；纯实现优化不得通过调整这些值获得收益。预计算表的实际启用状态须从运行时确认。
- 单个候选使用默认关闭的特性开关，保留通用回退；性能结论须来自冻结条件下的实际对照，静态汇编变化只证明实现形态。
- 已有实验仅在源码、配置和平台条件相关时作为参考；无记录时依据当前证据继续。

## 参考资料

- 调用链：[源码入口](references/ivfpq-algorithm-and-source.md)
- 参数、布局或机制：[布局与适用条件](references/ivfpq-parameters-and-data-layout.md)
- 测试映射：[验证与测试配置](references/ivfpq-sra-test-profile.md)
- 采样归因：[热点检查项](references/ivfpq-performance-hotspots.md)
- 候选设计：[候选与验证](references/ivfpq-optimization-candidates.md)
- 冻结字段：[benchmark-contract.json](references/benchmark-contract.json)

公共构建、源码、采样、SIMD 和测试方法分别使用 `faiss-build`、`faiss-source-analysis`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test` 和 `vector-ut-st-validation`。
