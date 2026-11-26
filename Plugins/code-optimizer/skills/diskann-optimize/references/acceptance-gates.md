# DiskANN 验收门禁

## G1 范围

- 只包含一个主要假设。
- 不隐藏参数、cache、warmup、数据集或编译器变化。
- feature-off 路径存在并完成测试。

## G2 安全与正确性

- 构建和 smoke test 通过。
- Recall 符合冻结契约。
- 对纯实现改动，IOs、Comps、Hops 保持一致。
- 涉及时，乱序 I/O、tail、错误路径和 buffer 生命周期正确。

## G3 性能证据

- 使用平衡运行顺序或等价漂移控制。
- 保留所有有效样本。
- 报告各数据集中位数和收益。
- 根据比值计算几何平均。
- baseline 不存在显著漂移，否则重新运行。

## G4 可复现性

- 归档环境、Manifest、构建命令、二进制路径/hash、commits、原始日志、patch 和验收报告。
- 复用历史基线时，必须由契约 hash 和 sentinel 检查证明合理。

## G5 知识固化

- 记录 accepted 机制及适用限制。
- neutral/failed 候选记录失败原因和值得复测的条件。
