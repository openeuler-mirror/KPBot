---
name: faiss-rabitq-optimization
description: 分析和优化 Faiss RaBitQ 索引的查询预处理、量化码字、距离估计和扫描路径。适用于 IndexRaBitQ/IndexIVFRaBitQ 及其 FastScan 变体的源码调查、热点归因与候选设计。
---

# Faiss RaBitQ 优化

## 输入

目标源码中存在的 RaBitQ 类、量化位数与查询精度参数、metric、索引与数据身份、查询模式和采样结果。
源码、构建选项和运行时行为以目标检出为准；版本号本身不能证明某个扩展内核或后端存在。

## 执行步骤

1. 确认目标版本提供的索引、量化器和 decoder；从调用方区分普通扫描与 FastScan 变体。
2. 分开记录数据码字位宽、查询量化位宽、旋转或中心化、校正因子及可选精排参数。
3. 追踪 set_query、位平面生成、位运算/点积和结果选择，结合热点判断预处理、解码或归约是否值得优化。
4. 保持码字格式、量化含义和距离校正；验证位宽、维度、非整字节尾部及 SIMD 回退后对照测量。

## 输出

源码与布局说明、带采样依据的热点归因、候选机制及适用条件、正确性/性能验证计划。完整实验由 `faiss-optimize` 编排；本技能提供算法专属判断。

## 约束

- 参数范围和布局从实际类型定义及编码函数取证。新增缓存或布局会影响索引格式与内存足迹，应纳入方案和兼容性验证。
- 单个候选使用默认关闭的特性开关，保留通用回退；性能结论须来自冻结条件下的实际对照，静态汇编变化只证明实现形态。
- 已有实验仅在源码、配置和平台条件相关时作为参考；无记录时依据当前证据继续。

## 参考资料

- 调用链：[源码入口](references/rabitq-algorithm-and-source.md)
- 参数、布局或机制：[布局与适用条件](references/rabitq-parameters-and-data-layout.md)
- 测试映射：[验证与测试配置](references/rabitq-sra-test-profile.md)
- 采样归因：[热点检查项](references/rabitq-performance-hotspots.md)
- 候选设计：[候选与验证](references/rabitq-optimization-candidates.md)
- 冻结字段：[benchmark-contract.json](references/benchmark-contract.json)

公共构建、源码、采样、SIMD 和测试方法分别使用 `faiss-build`、`faiss-source-analysis`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test` 和 `vector-ut-st-validation`。
