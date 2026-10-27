---
name: vector-ut-st-validation
description: 为向量检索实现改动设计和核验单元/系统测试，覆盖实际路径、特性开关、边界、回退、并发和集成。适用于候选实现完成后及性能验收前。
---

# 向量检索 UT/ST 验证

## 输入

候选方案与 diff、基线/候选构建、风险清单、数值契约、目标平台和真实测试入口。

## 执行步骤

1. 从受影响行为建立 UT/ST 矩阵，读取 [测试契约](references/test-contract.md)。
2. 对实际模块/API 验证 feature OFF/ON、guard 命中与回退；覆盖尺寸、尾段、非对齐和相关错误路径。
3. 按改动风险补充并发、内存、I/O、数值或兼容性测试；不适用项说明理由，未执行的工具说明替代证据与限制。
4. 在目标平台运行至少一次系统集成测试，比较结果 ID、距离、质量和相关工作量。
5. 写入测试报告并运行 `scripts/validate_test_report.py <report.json>`，通过后进入正式性能对照。

## 输出

测试矩阵、原始日志、结果差异和符合 [报告 Schema](references/test-report.schema.json) 的 JSON。

## 约束

Benchmark Recall 相同不能替代功能测试；不以性能结果覆盖失败用例。使用真实实现差分验证，避免复刻候选算法作为唯一判据。
