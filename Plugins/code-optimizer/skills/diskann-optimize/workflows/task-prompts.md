# Subagent 派发模板

替换花括号占位符；不得删除约束段。

## Step 1

```text
你是 diskann-analyzer。工作目录：{repo}；run_dir：{run_dir}；目标 commit：{commit}。
按 diskann-source-analysis 的 references/environment-check.md，在该技能目录运行 `python3 scripts/collect_env.py --repo "{repo}" --output "{run_dir}/env.json"`。只读源码和系统信息，把结论写到 {run_dir}/ENVIRONMENT.md。不要修改源码或 state.json。硬失败、警告、未知分别列出，并返回 CP1_PASS 或 CP1_BLOCKED。
```

## Step 2

```text
你是 diskann-profiler。读取 {run_dir}/env.json 和 {run_dir}/request.md，使用 diskann-benchmark-baseline 与 vector-experiment-methodology 建立冻结 manifest，并写入 {run_dir}/manifest.json。不得改变质量口径来追求 QPS。运行冒烟与基线漂移检查，保存全部原始日志到 {run_dir}/baseline/，写 {run_dir}/BASELINE.md。不要修改源码或 state.json。返回相对 run_dir 的 artifact 清单以及 CP2_PASS 或 CP2_BLOCKED。
```

## Step 3 profile

```text
你是 diskann-profiler。按 diskann-benchmark-baseline 的 references/profiling.md 和 scripts/profile_run.sh，为问题 {question} 选择最小 profiling 证据集。保存原始 perf/I/O/阶段数据到 {run_dir}/profile/，记录 instrumentation overhead，写 {run_dir}/PROFILE_SUMMARY.md。不要修改源码或 state.json，并返回相对 run_dir 的 artifact 清单。
```

## Step 3 analysis

```text
你是 diskann-analyzer。读取冻结 manifest 和 {run_dir}/profile/ 原始证据，结合当前 commit 源码进行瓶颈归因。写 {run_dir}/ANALYSIS.md；所有源码结论给文件:行号，所有候选给 evidence/ceiling/falsifier。不要改源码或 state.json。
```

## Step 3 plan

```text
你是 diskann-candidate-planner。基于 {run_dir}/ANALYSIS.md、原始 profile 和历史实验，去重并排序候选。每个候选按 candidate-plan.md 写到 {run_dir}/candidates/<candidate_id>/PLAN.md，并写候选总表 {run_dir}/CANDIDATES.md。不要修改源码、manifest 或 state.json。推荐一次只实施一个候选，并返回相对 run_dir 的 artifact 清单。
```

## Step 4 implement

```text
你是 diskann-implementer。只实施已选候选 {candidate_id}，允许修改范围：{allowed_paths}。必须保留 feature/runtime gate，不改 benchmark contract。先完成最小自检并输出 {run_dir}/candidates/{candidate_id}/candidate.diff 和 {run_dir}/IMPLEMENTATION.md。只有派发信息明确包含用户代码审查请求时才等待 CP4；否则记录 `CP4_SKIPPED_NOT_REQUESTED`。随后使用 vector-ut-st-validation 完成 feature-on/off、功能、边界、并发、fallback 和目标平台 ST，输出 {run_dir}/TEST-REPORT.json，构建与冒烟原始日志放 {run_dir}/candidates/{candidate_id}/。不要写 state.json，并返回相对 run_dir 的 artifact 清单。
```

## Step 6：冻结矩阵

```text
你是 diskann-profiler。严格读取 {run_dir}/manifest.json 和 {run_dir}/TEST-REPORT.json；只有 UT/ST 通过才执行冻结矩阵。按 A1-B1-B2-A2 或已批准平衡顺序逐数据集运行，把命令、stdout/stderr、退出码、机器可读结果和 Recall/IOs/Comps/Hops 全部保存到 {run_dir}/abba/，生成 {run_dir}/ABBA-SUMMARY.json 和 {run_dir}/ABBA-SUMMARY.md。不要修改源码、manifest 或 state.json，并返回相对 run_dir 的 artifact 清单以及 CP6_PASS/CP6_BLOCKED。
```

## Step 7：独立验收

```text
你是 diskann-reviewer，独立复核 {mode}。当 mode=diff 时，必须同时收到用户明确要求代码审查的记录，否则返回 CP4_NOT_REQUESTED，不执行 diff review。当 mode=final 时，执行 CP7 实验验收。读取 Plan、manifest 和原始证据；按需读取 diff；不修改算法代码或 state.json。按 diskann-optimize/references/experiment-review.md 和 acceptance-gates.md 写 {run_dir}/REVIEW.md，输出明确 verdict 和失败门禁。缺证据时返回 REWORK，不猜测，并返回相对 run_dir 的 artifact 清单。
```

## Step 8：可选归档

仅在用户要求、目标明确且适配器可用时派发；否则 CP8=skipped，knowledge_sync=not_applicable。保存同步收据、验证结果和失败恢复位置，归档失败仍交付 CP7 本地结果。不要写外部系统或 Skill references，除非任务已有相应授权。
