# DiskANN 性能优化工作流

本工作流实现 [通用向量检索 CP0–CP7](../../vector-search-optimize/references/cp0-cp7.md) 的 DiskANN 专项步骤。由通用入口转入时，沿用传入的范围和已有实验状态。

## CP0：范围和初始化

确认源码/补丁、Rust 或 C++、内存或 SSD、性能目标、测试入口、数据/索引和允许资源。创建独立 run_dir：

```bash
python workflows/scripts/init_run.py --workflow diskann-optimize \
  --run-id exp-example --repo /path/to/DiskANN --run-dir /path/to/runs/exp-example
```

也可用 `--experiment-root` 与 `--experiment-id` 派生目录。Wiki 仅在用户要求同步时提供 `--wiki-root`；记录可用适配器与授权，不能默认为外部写入授权。写 request.md。非 Git 发布源码可调查，CP2 前必须补齐源码摘要、版本、补丁和可复现命令。

## CP1：环境与源码

Analyzer 使用 diskann-source-analysis 的环境与源码流程（含 scripts/collect_env.py）确认当前入口、语言、后端、构建选项和资源。输出 env.json、ENVIRONMENT.md、SOURCE_MAP.md。仅目标依赖不满足才阻断：一般调查不要求 AArch64、SSD、perf 或 Git；鲲鹏实机验收则必须在声明的平台完成。

## CP2：冻结基线

Profiler 使用 diskann-benchmark-baseline 创建 manifest，保存真实构建命令、源码/二进制/数据/索引摘要、有效参数、计时公式、Recall 定义、预热、重复次数和漂移阈值。至少两次基线检查，正确性冒烟通过才冻结。输出 manifest.json、BASELINE.md、baseline/。原生测试程序优先，随包性能 case 仅用于 Rust 内存图的基准冒烟。

## CP3：采样、归因与候选

Profiler 按 diskann-benchmark-baseline/references/profiling.md 保存 profile/ 与 PROFILE_SUMMARY.md；Analyzer 按 diskann-source-analysis/references/bottleneck-diagnosis.md 关联当前源码输出 ANALYSIS.md；Planner 按需读取本技能的 I/O、SIMD 或布局参考资料，写 CANDIDATES.md 与 candidates/<id>/PLAN.md。每个候选包含证据、收益上限、风险、验证实验和证伪条件。已有历史记录按适用条件使用，缺少记录不阻断。

## CP4：实现和按需审查

按 pipeline_mode 和已有授权选择候选，Implementer 实现单一主要机制并保留默认关闭的 feature/runtime gate。记录 IMPLEMENTATION.md、candidate.diff、最小自检。只有用户请求代码/diff 审查才追加独立 CP4 审查；否则记录 CP4_SKIPPED_NOT_REQUESTED 并继续构建。失败时记录失败证据、返回阶段和有界修复计划；连续两轮无进展则交付受阻结论。

## CP5：构建、正确性和 UT/ST

使用 vector-ut-st-validation 验证开关双向、功能、边界、并发、fallback 和目标平台 ST。报告 TEST-REPORT.json，保存构建日志和冒烟原始输出。benchmark Recall 不变不能替代 UT/ST；缺目标硬件测试不得声称通过目标硬件验收。

## CP6：交错性能复验

沿用冻结矩阵运行 A1-B1-B2-A2 或等价平衡序列，保存 abba/ 下每轮命令、stdout/stderr、退出码、原始指标与先声明的异常处理。记录 Recall、Comps、Hops；SSD 路径另外记录 IOs，内存路径不伪造 IOs。使用 vector-experiment-methodology 汇总，不把描述性比较直接当作验收结论。

## CP7：独立验收与本地交付

Reviewer 按本技能 references/experiment-review.md 与 references/acceptance-gates.md 复核 Plan、manifest、正确性、全部有效样本、漂移、逐负载收益和原始证据，输出 REVIEW.md。结论为 ACCEPT、ACCEPT_WITH_NOTES、REJECT、INCONCLUSIVE；REWORK 是返回实现或测试的非终态，重置受影响阶段并记录原因。

只在 ACCEPT/ACCEPT_WITH_NOTES 且已授权时本地提交并保存完整 SHA。其余记录 not_authorized 或 not_applicable；自动模式不授予提交、推送和发布权限。协调者交付本地实验路径、结论、限制和未完成验证。

## CP8：可选知识同步

默认 skipped / not_applicable。仅在用户要求且可用适配器与目标明确时启用。预检路径和冲突后，按目标知识库规范同步并保存 receipt 与 validation_receipt；不能覆盖冲突档案。失败记录 blocked/failed 和恢复锚点，明确 CP7 已完成与同步尚未完成的各自状态，继续交付本地报告。

每阶段更新后执行 validate_state.py。子角色只回传 [结构化交接](references/handoff-contract.md)，主协调者写状态。
