---
name: diskann-benchmark-baseline
description: 建立和核验 DiskANN 冻结基线，运行原生性能用例并采集 CPU/I/O 证据。用于准备源码与二进制、数据/索引、有效查询参数和运行条件，测量 QPS/延迟/Recall 或为瓶颈分析生成 profile。
---

# DiskANN 基线、性能测试与采样

## 输入

当前源码与实际构建、测试程序、数据/索引、ground truth、允许资源、指标与 run_dir。环境调查由 diskann-source-analysis 提供。

## 工作流程

1. 识别 Rust/C++ 和内存/SSD 路径，按 [基线契约](references/baseline-contract.md) 记录源码、构建命令、flags、二进制和数据/索引摘要。
2. 从 [manifest 示例](references/experiment-manifest.example.json) 建立本次清单，替换占位符，执行 `scripts/validate_manifest.py`；从实际测试输出核对参数和计时公式。
3. 先运行正确性冒烟，再按预先声明的预热、样本数和漂移规则建立基线。Rust 内存图可直接使用 [原生性能用例](references/performance-case.md)，不把该用例扩展为 SSD 或生产负载的性能保证。
4. 需要定位热点时按 [采样流程](references/profiling.md) 和 [工具矩阵](references/profile-matrix.md) 选择最小证据集，执行 `scripts/profile_run.sh`，保存原始命令、计数、日志和开销说明。
5. 冻结后变更条件须建立新 baseline_id。候选对照复用已有 vector-experiment-methodology；实现后的 UT/ST 使用已有 vector-ut-st-validation。将采样交给 diskann-source-analysis 归因，验收交给 diskann-optimize。

## 输出

manifest.json、构建与身份记录、原始性能样本、BASELINE.md；按需增加 profile/ 和 PROFILE_SUMMARY.md。

## 约束

保持数据、索引、查询质量、资源与公共编译条件可比；保留全部有效样本，采样吞吐不代替正式对照。perf 不可用时明确缺口，`--perf required` 阻止缺计数运行；不得静默重跑工作负载。公共统计与 UT/ST 实现不在本包复制。
