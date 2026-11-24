---
name: faiss-optimize
description: 组织 Faiss 性能优化任务：识别索引类型，编排源码分析、基线、采样、候选实现、正确性验证和性能验收。适用于完整优化流程；参数解释或单独源码调查按需调用对应技能。
---

# Faiss 性能优化工作流

主协调者负责范围、算法路由、角色交接和状态管理。技术结论来自当前源码、实际构建产物和本次实验。

## 输入

从请求和工作区获取源码路径、目标算法、性能目标、测试工具、数据集与产物目录 `run_dir`。已有补丁、实验记录和交互模式为可选输入。只询问影响下一步且无法从上下文确认的信息。

## 执行步骤

1. 确定任务范围：参数/API 问题直接调用算法技能；源码调查调用 `faiss-analyzer`；完整优化进入 [CP0–CP7](workflows/optimize-workflow.md)。
2. 按下表选择一个 `active_algorithm_skill`。多算法任务分别建立实验；未覆盖的索引可用公共分析技能调查，完整工作流只接受状态协议支持的算法。
3. 继承 KPBot 的 `pipeline_mode`；直接调用默认 `collaboration`。`auto` 自主排序并记录候选选择；`collaboration` 在选定候选时确认用户意图。已有授权持续有效，提交权限单独判断。
4. 创建或恢复 `run_dir/state.json`。只有主协调者更新状态；角色通过 handoff 接收任务、回传结果。每次状态或交接变更运行对应校验器。
5. 基于真实热点选择一个主要机制，完成默认关闭的实现、开关双向验证和交错性能对照，再交独立验收员判定。

| 算法 | 技能 |
| --- | --- |
| `IndexRaBitQ` / `IndexIVFRaBitQ` | `faiss-rabitq-optimization` |
| TurboQuant / `QT_4bit_tq`（源码支持时） | `faiss-turboquant-optimization` |
| `IndexIVFPQ` | `faiss-ivfpq-optimization` |
| `IndexIVFPQFastScan` / ivfpqfs / pqfs | `faiss-ivfpqfs-optimization` |
| `IndexHNSW*` | `faiss-hnsw-optimization` |

| 角色 | 阶段与交付 |
| --- | --- |
| `faiss-analyzer` | CP1：`SOURCE-EVIDENCE.md` |
| `faiss-profiler` | CP2 / CP3-A / CP6：`PROFILE-SUMMARY.md`、原始记录 |
| `faiss-candidate-planner` | CP3-B：`CANDIDATE-PLAN.md` |
| `faiss-implementer` | CP4 / CP5：`CHANGES.md`、构建与 UT/ST 证据 |
| `faiss-reviewer` | CP7：`EXPERIMENT-REPORT.md`、verdict |

## 输出

实验状态、源码证据、冻结 manifest、候选补丁、构建与测试报告、性能原始记录及验收结论。阶段产物写入独立 `run_dir`，源码改动留在目标仓库；通用技能目录不保存具体任务的实验结果。

## 约束

- 区分上游能力、目标仓库的扩展和待验证假设；按实际检出识别版本、补丁、内核与后端。
- 参数、数据、索引、公共编译条件和运行环境按契约冻结。纯实现优化默认要求 Recall 不下降；质量/速度权衡须另行声明并建立可比实验。
- 已有实验记录仅在来源和适用条件明确时使用；缺少记录不阻断启动。候选结论保存到 `run_dir/candidates/<id>/VERDICT.md`。
- ACCEPT/ACCEPT_WITH_NOTES 后，仅按用户授权执行本地提交并记录完整 SHA；未授权记 `not_authorized`。REJECT/INCONCLUSIVE 记 `not_applicable`；REWORK 按指定阶段返回。自动模式不授予提交、推送或发布权限。
- 保留已有工作和原始证据；资源分配与操作范围遵守当前任务约束。

## 参考资料

- 初始化与恢复：[quickstart.md](references/quickstart.md)
- 阶段编排：[optimize-workflow.md](workflows/optimize-workflow.md)
- 状态与交接：[state-template.md](workflows/references/state-template.md)
- 公共能力：`faiss-source-analysis`、`faiss-build`、`faiss-benchmark-baseline`、`faiss-perf-profile`、`faiss-simd-optimization`、`faiss-sra-test`、`faiss-algorithm-optimization`、`vector-experiment-methodology`、`vector-ut-st-validation`。
