# Faiss 完整优化流程（CP0–CP7）

状态协议：[state.schema.json](references/state.schema.json)。交接协议：[handoff.schema.json](references/handoff.schema.json)。本流程由 primary 协调，阶段产物写独立 run_dir，代码改动写目标源码仓。

| 阶段 | 执行者 | 完成条件 |
| --- | --- | --- |
| CP0 初始化 | primary | 输入、模式、资源与停止条件明确，state 有效 |
| CP1 源码调查 | faiss-analyzer | SOURCE-EVIDENCE.md 及 analysis-result |
| CP2 基线 | faiss-profiler | 冻结 manifest、身份、质量与性能记录 |
| CP3 热点与规划 | faiss-profiler → faiss-candidate-planner | 当前热点、候选计划和 selected_candidate |
| CP4 实现 | faiss-implementer | 默认关闭的候选、补丁、CHANGES.md 与自审 |
| CP5 构建和 UT/ST | faiss-implementer | OFF/ON 构建和实际路径测试通过 |
| CP6 性能对照 | faiss-profiler | 完整交错测量、质量与漂移检查 |
| CP7 验收交付 | faiss-reviewer → primary | 独立 verdict、报告和授权范围内的交付 |

## CP0 初始化与恢复

从请求/工作区确认 repo、算法、测试工具、数据/索引、run_dir、资源边界和 interaction_mode。已有补丁和实验记录是可选输入；没有记录可直接开展调查。
用户给定路径优先；未指定 run_dir 时，在许可工作区选择独立产物目录并记录。使用 `scripts/init_run.py` 创建状态；已有状态先读取并校验，不覆盖已完成阶段。
源码身份包括 commit、工作区改动和已有补丁。`--patch` 只记录路径，primary 另算 SHA256 写 manifest。记录目标、候选/测量预算与停止条件，避免无界重试。

## CP1 源码调查

派发 analysis-request，确认实际索引、包装器、参数、数据布局和内核分派。区分基础能力、可选扩展及未知项；以 analysis-result 交付源码证据。环境问题在授权范围内修复，需新增权限或关键输入时报告阻塞。

## CP2 基线

派发 baseline-request。按目标算法契约记录源码/构建/库、数据与 ground truth、索引、查询和运行条件，并预声明统计规则。记录实际加载库与线程/CPU/NUMA 行为。
基线身份与可比性信息不足时，不虚假完成 CP2；注明缺失内容。已有图或量化索引的查询对照优先复用同一保存实例，构建任务另行设计对照。

## CP3 热点与候选

CP3-A 派发 profile-request：一次采一个声明的工作负载，记录事件、采样分母、调用栈和源码/汇编映射。`perf record -F 99 -g --call-graph fp` 是可用时的起点，权限和栈条件不同则记录替代方案。
CP3-B 派发 plan-request：按热点可移除成本、适用 guard、额外开销和风险生成 CANDIDATE-PLAN.md。一个候选一个主要机制。已有记录仅在来源和条件相符时参考。
`auto` 自主选择并记录理由；`collaboration` 确认候选选择。写 selected_candidate 后完成 CP3。

## CP4 实现

派发 implement-request。按计划最小改动、默认关闭、保留 fallback，不改冻结字段或未授权语义；设计不可行时返回 CP3 而非自行扩大方案。
Implementer 完成自审、补丁和 CHANGES.md。用户要求单独 diff 审查时，再派 reviewer 只审不改；未要求则在 decisions 记 `CP4_SKIPPED_NOT_REQUESTED`，仅表示该可选审查未请求，CP4 实现照常完成。

## CP5 构建与功能验证

在独立目录进行 OFF/ON 构建，记录真实宏、flags、依赖和产物身份。根据 diff 风险选择实际 API 的边界、guard/fallback、数值、并发和兼容性测试，写有效 UT/ST 报告。失败保留证据并修复；不能修复或到达预算时报告阻塞。

## CP6 正式对照

派发 abba-request。运行前复核冻结字段；按预声明的 A1–B1–B2–A2 或更长平衡序列运行，A 为参考、B 为候选。数据集矩阵、预热、缓存策略、次数、指标公式和漂移阈值在看结果前确定。
保留每次有效结果和实际环境证据。记录全部质量差异、离散程度及 A 臂漂移；不挑最快结果，不静默排除数值异常。冻结条件变更建立新实验，旧数据不能并入。

## CP7 独立验收与交付

派发 review-request，独立 reviewer 核对源码范围、开关、构建/质量证据、对照和漂移，写 EXPERIMENT-REPORT.md。
- ACCEPT/ACCEPT_WITH_NOTES：通过且收益有证据；primary 按用户授权执行本地提交，记录完整 SHA。未授权则 candidate_commit.status=not_authorized 并列 pending_files。
- REJECT/INCONCLUSIVE：保留证据，candidate_commit.status=not_applicable，不为凑结论提交。
- REWORK：提供 return_to、reason 和 required_actions；primary 记录 rework_events，将目标及后续阶段退回 pending 后重走。它不是终态 verdict。
终态写 result.verdict 与 report 并完成 CP7。自动模式不推导提交、推送或发布权限。

## 状态纪律

只有 primary 更新 state；阶段按 0–7 形成 completed 前缀，后面至多一个 running/blocked/failed。每次状态与 handoff 变更使用对应校验器。
中断后对照实际文件和状态恢复，不将未完成任务当作完成。保留用户修改、失败尝试和原始数据；未经授权不重启环境或清理目录。资料或证据缺失如实记录，不补造结果。
