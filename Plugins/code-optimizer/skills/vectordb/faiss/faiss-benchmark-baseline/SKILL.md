---
name: faiss-benchmark-baseline
description: 建立和核对 Faiss 性能实验基线，冻结源码、构建、数据、索引、查询参数和运行条件。适用于实现对照前的基线准备、复用检查与参数漂移排查。
---

# Faiss 实验基线

## 输入

源码与构建身份、测试工具、数据与 ground truth、保存索引、查询配置和运行环境。

## 执行步骤

1. 确认基线源码、补丁和实际加载库；以当前证据生成 `baseline_id`。
2. 按算法契约记录冻结字段，包含包装索引、运行时覆盖参数、并发方式、CPU/NUMA 和依赖线程配置。
3. 预先声明预热、缓存策略、重复次数、指标公式和漂移处理；保留全部有效测量。
4. 保存 QPS/延迟、Recall、原始结果和身份校验，复用时重新检查可比性。

## 输出

`manifest.json`、基线测量、数据/索引/库哈希及环境与参数证据。

## 约束

基线由当前任务定义，不预设版本、补丁、数据集矩阵或处理器。字段变化应建立新实验；显著环境漂移下的旧结果不能替代当前对照。

## 参考资料

- [基线契约](references/baseline-contract.md)
- [测试字段映射](references/sra-test-baseline-fields.md)
- [漂移与复用](references/baseline-history.md)
- [结果比较](references/result-comparison.md)
- 各算法的 `references/benchmark-contract.json`
