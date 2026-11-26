---
name: pgvector-optimize
description: 组织 pgvector 性能优化任务：识别索引类型与距离内核，编排源码分析、基线、采样、候选实现、正确性验证和性能验收。适用于完整优化流程；参数解释或单独源码调查按需调用对应技能。
---

# pgvector 性能优化工作流

主协调者负责范围、算法路由、角色交接和状态管理。技术结论来自当前源码、实际构建产物和本次实验。

## 输入

从请求和工作区获取源码路径、目标（HNSW/IVFFlat 索引、距离内核或构建路径）、性能目标、测试工具、数据与产物目录 `run_dir`。已有补丁、实验记录和交互模式为可选输入。只询问影响下一步且无法从上下文确认的信息。

## 执行步骤

1. 确定任务范围：参数/API 问题直接回答或调用对应技能；源码调查调用 `pgvector-analyzer`；完整优化进入 [CP0–CP7](workflows/optimize-workflow.md)。
2. 按下表选择一个 `contract.algorithm`。多目标任务分别建立实验；未覆盖的目标可用公共分析技能调查，完整工作流只接受状态协议支持的目标。
3. 继承 KPBot 的 `pipeline_mode`；直接调用默认 `collaboration`。`auto` 自主排序并记录候选选择；`collaboration` 在选定候选时确认用户意图。已有授权持续有效，提交权限单独判断。
4. 创建或恢复 `run_dir/state.json`。只有主协调者更新状态；角色通过 handoff 接收任务、回传结果。每次状态或交接变更运行对应校验器。
5. 基于真实热点选择一个主要机制，完成默认关闭的实现、开关双向验证和交错性能对照，再交独立验收员判定。

| 实际优化目标 | contract.algorithm |
| --- | --- |
| HNSW 索引（查询或构建） | `hnsw` |
| IVFFlat 索引（查询、构建或 k-means 训练） | `ivfflat` |
| vector / halfvec / bit / sparsevec 距离与转换内核 | `distance-kernel` |
| 精确最近邻、距离算子与聚合 | `exact-scan` |

| 角色 | 阶段与交付 |
| --- | --- |
| `pgvector-analyzer` | CP1：`SOURCE-EVIDENCE.md` |
| `pgvector-profiler` | CP2 / CP3-A / CP6：`PROFILE-SUMMARY.md`、原始记录 |
| `pgvector-candidate-planner` | CP3-B：`CANDIDATE-PLAN.md` |
| `pgvector-implementer` | CP4 / CP5：`CHANGES.md`、构建与 UT/ST 证据 |
| `pgvector-reviewer` | CP7：`EXPERIMENT-REPORT.md`、verdict |

## 输出

实验状态、源码证据、冻结 manifest、候选补丁、构建与测试报告、性能原始记录及验收结论。阶段产物写入独立 `run_dir`，源码改动留在目标仓库；通用技能目录不保存具体任务的实验结果。

## 约束

- 区分上游能力、目标仓库的扩展和待验证假设；按实际检出识别版本、补丁、内核与后端。pgvector 是 PostgreSQL 扩展：核对 `EXTVERSION`、`Makefile`/`Makefile.win` 的编译宏（`OPTFLAGS`、自动向量化标志、`HALFVEC_DISPATCH`/`BIT_DISPATCH`/`VECTOR_TARGET_CLONES`）、实际 Postgres 版本与平台（read_stream / prefetch 按版本分派）。
- 参数、数据、索引、公共编译条件和运行环境按契约冻结。纯实现优化默认要求 Recall 不下降；质量/速度权衡须另行声明并建立可比实验。索引复用优先使用同一保存实例。
- 已有实验记录仅在来源和适用条件明确时使用；缺少记录不阻断启动。候选结论保存到 `run_dir/candidates/<id>/VERDICT.md`。
- ACCEPT/ACCEPT_WITH_NOTES 后，仅按用户授权执行本地提交并记录完整 SHA；未授权记 `not_authorized`。REJECT/INCONCLUSIVE 记 `not_applicable`；REWORK 按指定阶段返回。自动模式不授予提交、推送或发布权限。
- 保留已有工作和原始证据；资源分配与操作范围遵守当前任务约束。

## 参考资料

- 初始化与恢复：[quickstart.md](references/quickstart.md)
- 阶段编排：[optimize-workflow.md](workflows/optimize-workflow.md)
- 状态与交接：[state-template.md](workflows/references/state-template.md)
- 公共能力：`vector-experiment-methodology`、`vector-ut-st-validation`、`vector-search-optimize`，以及通用 `apply-vectorization`、`prefetch-optimization`、`memory-access-optimization`、`operation-fusion`、`compiler-flag-tuning`、`special-case-optimization`。
