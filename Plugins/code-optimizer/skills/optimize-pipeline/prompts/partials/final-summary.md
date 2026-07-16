## 最终输出格式

流水线完成后，向用户报告：

### 成功（多轮）

```
## 优化流水线完成（共 ${total_rounds} 轮）

### 总体概况
| 轮次 | 优化点数 | 已应用 | 已跳过 | 本轮提升 |
|------|---------|--------|--------|---------|
${round_summary_rows}

### 各轮详情

${per_round_details}
```

其中 `round_summary_rows` 每行格式：
`| ${round.round} | ${round.optimization_points_total} | ${round.applied_count} | ${round.skipped_count} | ${round.round_speedup} |`

`per_round_details` 为每轮展开：

```
#### 第 ${round} 轮
| # | 函数 | 状态 | 性能变化 | 说明 |
|---|------|------|---------|------|
| ${result_table_rows} |

- Profiling 工具: ${profiling_tool}
- TopN: ${profiling_topn}

### 运行环境
- 架构: ${machine_arch}
- CPU: ${machine_cpu_model}
- 平台匹配: ${machine_platform_match}
${machine_warning}

### 跳过的函数
- 跳过的函数: ${skipped_items}
```

其中 `result_table_rows` 由 `round.sub_task_results` 生成，每行格式：
`| ${sub_task.id} | ${sub_task.function} | ${sub_task.status} | ${sub_task.speedup} | ${sub_task.fix_info} ${sub_task.description} |`

`machine_arch`、`machine_cpu_model`、`machine_platform_match`、`machine_warning` 从 `context.prepareProject.machine` 提取。

`sub_task_results` 元素结构（由协调者在函数级收尾时从 `optimization_point_results` 汇总生成）：
- `id`：子任务编号（来自 decompose-tasks 的 sub_tasks[].id）
- `function`：函数名
- `status`：综合状态，取优化点最差结果（`verified` > `marginal` > `unverified` > `skipped` > `failed`）
- `speedup`：最佳优化点的性能提升（如 `"1.5x"`），无可优化点时为空
- `fix_info`：若该函数任何优化点触发过 fix-code，显示 `"修复${N}轮 "`；未触发时为空
- `description`：来自 decompose-tasks 的 sub_tasks[].reason

### 成功（单轮，仅一轮时使用简化格式）

```
## 优化流水线完成

### 子任务汇总
| # | 函数 | 状态 | 性能变化 | 说明 |
|---|------|------|---------|------|
| ${result_table_rows} |

### Profiling
- 工具: ${profiling_tool}
- TopN: ${profiling_topn}

### 跳过的函数
${skipped_items}
```

### 持续进化
流水线运行结束后，向用户展示以下提示：
```
> 本次流水线共 ${total_rounds} 轮，发现 ${total_optimization_points} 个优化点，应用 ${applied_count} 个，跳过 ${skipped_count} 个。
> 如果发现了新的优化模式或有改进现有策略的建议，可随时调用 `/evolve-skill` 将其编码入库，让下次运行自动受益。
```

其中 `total_optimization_points` 为所有轮次所有函数子任务的 `optimization_points` 总数，`applied_count` 为所有轮次中 status 为 `verified`/`marginal` 的数量，`skipped_count` 为所有轮次中 status 为 `skipped`/`failed` 的数量。

### Batch 机器可读结果

无论成功、阻塞还是无优化空间，结束前必须写：

`optimization_reports/run_<run_id>/batch_result.json`

最低字段：

```json
{
  "pipeline_status": "completed|blocked|failed",
  "quality_status": "applied_verified|applied_unverified|complete_no_optimization|baseline_blocked|pipeline_incomplete|artifact_error|report_inconsistent|driver_failed",
  "applied_count": 0,
  "verified_count": 0,
  "clean_patch_files": [],
  "blocked_reason": null,
  "performance_summary": null
}
```

当 `quality_status == "complete_no_optimization"` 时，`applied_count` 必须为 `0`，且不得存在源码/config patch。若 PrepareProject/baseline 被依赖、构建或测试阻塞，使用 `quality_status: "baseline_blocked"` 并同步写 `baseline_blocked.json`。

### 达到最大轮次上限

当 `round > max_rounds`（默认 5）时：
```
## 优化流水线结束

已达最大轮次上限（${max_rounds} 轮），自动停止。可能存在仍未消除的瓶颈，可手动重新运行流水线。
```

### BLOCKED
```
## 流水线阻塞

阶段: ${blocked_stage}
原因: ${reason}
可重试操作: ${suggestion}
```

## 阻塞处理规则

| 阶段 | 失败条件 | 处理 |
|------|---------|------|
| GatherContext | status: empty | BLOCKED，用户未提供有效信息 |
| ParseIntent | status: empty | 记录结论，继续（下游使用默认意图） |
| PrepareProject | status != ready | BLOCKED，报告原因 |
| DecomposeTasks（首轮） | status: empty | BLOCKED，无优化目标 |
| DecomposeTasks（第 2+ 轮） | status: empty | 正常退出轮次循环（性能瓶颈已消除） |
| 子任务 - AnalyzeHotspot | status: empty | 记录结论，跳过该子任务，继续下一个 |
| 子任务 - DecideOptimization | status: skipped | 记录结论，跳过该优化点，继续下一个 |
| 子任务 - ApplyOptimization | optimization_success == false | 路由到 fix-code 尝试修复；修复成功→re-verify；修复失败→清理中间件（git stash + 删除临时文件），记录失败，继续下一个 |
| 子任务 - VerifyOptimization | failed | 不 stash，路由到 fix-code 流程 |
| 子任务 - VerifyOptimization | regression | 变更已 stash，记录结果，继续下一个子任务 |
| 子任务 - FixCode | status: failed | git stash + 清理中间件，记录结果，继续下一个子任务 |
| 子任务 - 重新 VerifyOptimization | failed/regression | 变更已 stash，记录结果，继续下一个子任务 |
| 轮次循环 | round > max_rounds | 报告上限，正常退出 |
