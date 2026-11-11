---
name: diskann-source-analysis
description: 检查 DiskANN 源码与运行环境，追踪 Rust/C++ 构建或查询调用链，并结合已有 profile 诊断瓶颈。用于机制调查、环境准备或证据归因；性能测量使用 diskann-benchmark-baseline，完整实现优化使用 diskann-optimize。
---

# DiskANN 源码与瓶颈分析

## 输入

源码路径与身份、调查问题、目标平台和资源；已有基线、profile、论文或实验记录为可选输入。

## 工作流程

1. 固定当前检出或发布版本与源码摘要，识别 Rust/C++、内存图/SSD、量化与真实后端。
2. 需要环境盘点时，在本技能目录按 [环境检查](references/environment-check.md) 使用 `python3 scripts/collect_env.py`。根据 [平台检查表](references/kunpeng-checklist.md) 判断实际依赖。
3. 按 [证据协议](references/evidence-protocol.md) 从公开入口追踪参数、所有权、关键循环、序列化和系统调用。可在本技能目录用 `python3 scripts/source_evidence.py /path/to/DiskANN 'search|beam'` 生成符号索引。
4. 已有 profile 时按 [瓶颈分析](references/bottleneck-diagnosis.md) 和 [归因树](references/diagnosis-tree.md) 关联每查询工作量、CPU、等待、内存与 I/O；缺少采样则交由 diskann-benchmark-baseline 采集。
5. 输出绑定版本的文件/符号/行号，区分事实、推断和未知；候选写明证据、上限和证伪条件，交给 diskann-optimize 规划与实现。

## 输出

按任务产生 env.json、ENVIRONMENT.md、SOURCE_MAP.md / SOURCE-EVIDENCE.md 或 ANALYSIS.md，保存到独立 run_dir。

## 约束

分析不修改候选算法；构建/加载与查询、DRAM cache 与 SSD 读取、intrinsic 与实际指令分别判断。历史行号和热点不能替代当前证据，静态分析不能证明性能因果。通用测量和正确性规则消费已有 vector 技能。
