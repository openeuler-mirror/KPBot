---
name: faiss-hnsw-optimization
description: 分析和优化 Faiss HNSW 的图遍历、距离计算、visited 状态和候选容器。适用于 IndexHNSW 系列的参数与源码调查、查询或构建热点定位，以及保持图搜索语义的候选设计。
---

# Faiss HNSW 优化

## 输入

HNSW 索引与底层 storage 类型、M/efConstruction/efSearch、metric、构建或查询目标、保存索引身份和运行环境。
源码、构建选项和运行时行为以目标检出为准；版本号本身不能证明某个扩展内核或后端存在。

## 执行步骤

1. 追踪图构建或查询调用链，区分图结构、DistanceComputer、visited 状态、候选容器和结果处理。
2. 确认 storage 的真实精度及任何可选量化、批量距离或 ID 重排能力；它们不是所有 HNSW 实现共有的配置。
3. 结合采样和容器读写次数评估距离、邻接表访问、visited 初始化及维护成本，选择单一候选。
4. 优先复用同一保存图实例进行查询对照；验证 visited 生命周期、候选 tie/替换、并发与 ID 映射，再运行性能矩阵。

## 输出

源码与布局说明、带采样依据的热点归因、候选机制及适用条件、正确性/性能验证计划。完整实验由 `faiss-optimize` 编排；本技能提供算法专属判断。

## 约束

- 图构建可能受随机性和并发顺序影响。查询路径优化应冻结图实例；构建优化另行固定种子、顺序、线程与质量评估方法。
- 单个候选使用默认关闭的特性开关，保留通用回退；性能结论须来自冻结条件下的实际对照，静态汇编变化只证明实现形态。
- 已有实验仅在源码、配置和平台条件相关时作为参考；无记录时依据当前证据继续。

## 参考资料

- 调用链：[源码入口](references/hnsw-algorithm-and-source.md)
- 参数、布局或机制：[布局与适用条件](references/hnsw-optimization-mechanisms.md)
- 测试映射：[验证与测试配置](references/hnsw-validation-plan.md)
- 采样归因：[热点检查项](references/hnsw-performance-hotspots.md)
- 候选设计：[候选与验证](references/hnsw-optimization-candidates.md)
- 冻结字段：[benchmark-contract.json](references/benchmark-contract.json)

公共构建、源码、采样、SIMD 和测试方法分别使用 `faiss-build`、`faiss-source-analysis`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test` 和 `vector-ut-st-validation`。
