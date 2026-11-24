---
name: faiss-turboquant-optimization
description: 分析和优化目标 Faiss 实现中的 TurboQuant 距离估计、量化查表及查询预处理。适用于相关量化器、MSE/QJL 路径的源码调查、参数解释和性能候选设计；使用前确认目标源码支持这些类型。
---

# Faiss TurboQuant 优化

## 输入

支持 TurboQuant 的源码、量化器类型、数据与查询位宽、投影/旋转方式、metric、精排配置及热点证据。
源码、构建选项和运行时行为以目标检出为准；版本号本身不能证明某个扩展内核或后端存在。

## 执行步骤

1. 确认量化器枚举和实现类型是否存在；以实际注册与分派确认查询使用的路径。
2. 分解查询预处理、MSE 查表、可选 QJL 校正、预筛和精排，记录布局、因子、数值范围及参数映射。
3. 根据采样判断解码、gather、归约、缓存或寄存器压力，选择有边界条件和成本解释的候选。
4. 保持每段距离计算、误差界和候选筛选含义；覆盖维度、位宽、非有限值、尾部和后端回退。

## 输出

源码与布局说明、带采样依据的热点归因、候选机制及适用条件、正确性/性能验证计划。完整实验由 `faiss-optimize` 编排；本技能提供算法专属判断。

## 约束

- qb/int_qjl/nb_bits 等名称与含义依实现确认。改变量化精度、筛选阈值或精排规模属于算法权衡，不能当作等价实现收益。
- 单个候选使用默认关闭的特性开关，保留通用回退；性能结论须来自冻结条件下的实际对照，静态汇编变化只证明实现形态。
- 已有实验仅在源码、配置和平台条件相关时作为参考；无记录时依据当前证据继续。

## 参考资料

- 调用链：[源码入口](references/turboquant-algorithm-and-source.md)
- 参数、布局或机制：[布局与适用条件](references/turboquant-parameters-and-data-layout.md)
- 测试映射：[验证与测试配置](references/turboquant-sra-test-profile.md)
- 采样归因：[热点检查项](references/turboquant-performance-hotspots.md)
- 候选设计：[候选与验证](references/turboquant-optimization-candidates.md)
- 冻结字段：[benchmark-contract.json](references/benchmark-contract.json)

公共构建、源码、采样、SIMD 和测试方法分别使用 `faiss-build`、`faiss-source-analysis`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test` 和 `vector-ut-st-validation`。
