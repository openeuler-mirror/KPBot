# 迭代执行规则

本文件是 `workflow.md` 迭代执行阶段的展开版，定义候选 skill 列表的轮次循环、执行验证 subagent、输出要求、单 skill 停止判定和二进制验证规则。

## 单变量原则 / Single-Variable Principle

所有迭代优化必须遵守单变量原则：

- **定义**：每个执行轮次只能变更来自一个候选 skill 的变量。一个轮次对应一个 skill 的一组绑定动作，不得将多个 skill 的动作合并到同一轮次。
- **重启分离规则**：当多个 skill 的变更都要求同一次应用重启或系统重启时，仍必须拆分为独立轮次，每轮依次实施一个 skill 的变更，并在每轮间执行完整的基准验证（包括重启、warmup、压测和收益计算），以隔离每个 skill 的独立收益贡献。
- **不可归因标记**：若因实际限制（如无法在重启间执行压测、厂商固件捆绑更新等）必须合并执行，合并结果必须在 `per_skill_gain_summary` 中标记为 `confounded`，且 `attribution_method=merged_unresolvable`，并说明无法拆分的原因。
- **例外豁免**：仅允许在 `change_mode=online`（无需重启的动作）之间进行同一轮的多 skill 组合，且组合结果必须在报告中标注 `confounded`。组合仅限第一轮探索，后续必须拆分为独立轮次隔离验证。

## 迭代优化循环

基线确认、瓶颈识别、深度证据采集和 `candidate_skill_list` 生成后，主 skill 进入显式的轮次循环：

1. 从 `candidate_skill_list` 选择当前 skill，先执行 `phase=evidence_candidate`，再执行 `phase=coverage`。
2. 从该 skill 的 `candidate_pool.candidate_actions` 中选择本轮动作。
3. 校验本轮动作是否落在已确认的 `agent_action_mode`、`execution_authorization_scope`、权限范围、回退方式和验证窗口内。基线确认后不得按每个 skill 或每轮动作重复向用户询问批准；只有动作超出已确认边界、风险升级或需要新增权限时，才设置 `scope_change_confirmation_required=true` 并回到用户确认门控。
4. 启动一个执行验证 subagent，并把本轮候选动作、基线、上一轮有效配置、验证命令和回退条件写入任务包。
   生成任务包时必须传入当前 `per_skill_iteration_state`；若该 skill 已达到 5 轮上限，任务生成器必须拒绝继续生成执行任务。
5. 执行验证 subagent 确认只有自己会修改测试环境。
6. 执行验证 subagent 实施动作，或在动作超出授权范围时只输出 dry-run / 人工执行建议并标记 `blocked_scope_change_required`。
7. 执行验证 subagent 重新校验目标实例身份、资源约束、基线可比性和测试组网。
8. 执行验证 subagent 执行复测并落盘原始日志。
9. 执行验证 subagent 计算相对上一轮有效配置的阶段增量收益，以及相对初始干净基线的累计收益。
10. 若收益为负、风险超预期或身份/组网不一致，执行验证 subagent 回退该动作并记录为拒绝；后续轮次从上一轮有效配置继续。
11. 主 agent 读取执行验证 subagent 的 `round_N_summary.json`，更新该 skill 的 `per_skill_iteration_state`。
12. 若该 skill 仍可继续，进入该 skill 下一轮。
13. 若该 skill 触发停止条件，停止该 skill 并进入 `candidate_skill_list` 的下一个 skill。
14. 若所有主优化 skill 均完成、停止或阻塞并说明原因，进入报告、review、环境还原和案例归档。

## 每轮输出要求

每轮至少应记录：

- 当前轮次、当前已生效配置、当前轮候选动作池
- 本轮选中动作、本轮拒绝或暂缓动作
- 本轮主要证据、本轮累计收益
- 执行验证 subagent ID、任务包路径、原始日志路径和回退结果
- `execution_authorization_scope` 校验结果；若触发用户再确认，必须说明超出的具体边界
- 当前 skill 是否继续下一轮、停止原因、候选列表中的下一 skill

## per_skill_gain_summary 字段定义

每个候选和 coverage skill 完成后必须生成收益归因记录，汇总为 `per_skill_gain_summary` 数组：

```json
[
  {
    "skill_name": "cpu-affinity-optimization",
    "execution_order": 1,
    "rounds_attempted": 3,
    "stage_gains_pct": [2.1, 1.3, 0.5],
    "cumulative_gain_pct": 3.9,
    "attribution_method": "single_variable_round",
    "is_isolated": true,
    "confounded_with": [],
    "evidence_paths": ["rounds/round_1_summary.json"],
    "status": "stopped",
    "stop_reason": "five_rounds_gain_below_1_percent"
  }
]
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skill_name` | string | 是 | 子 skill 名称，必须与 `candidate_skill_list[].subskill_name` 一致 |
| `execution_order` | integer | 是 | 执行顺序编号，从 1 开始。cpu-affinity-optimization 必须为 1 |
| `rounds_attempted` | integer | 是 | 尝试轮次数 |
| `stage_gains_pct` | number[] | 是 | 每轮相对上一轮阶段收益百分比数组，与 `per_skill_iteration_state.round_gains_pct` 一致 |
| `cumulative_gain_pct` | number/null | 是 | 该 skill 全部轮次的独立累计收益（相对初始基线）；无法计算时为 null |
| `attribution_method` | string | 是 | 归因方法：`single_variable_round`（单变量轮次）/ `baseline_reset`（回基线验证）/ `merged_unresolvable`（合并不可拆分）/ `confounded`（存在混淆因子） |
| `is_isolated` | boolean | 是 | 该 skill 的收益是否可独立归因。`false` 时必须填写 `confounded_with` |
| `confounded_with` | string[] | 是 | `is_isolated=false` 时列出混淆的 skill 名称；`is_isolated=true` 时为空数组 |
| `evidence_paths` | string[] | 是 | 各轮轮次摘要路径 |
| `status` | string | 是 | `completed` / `stopped` / `blocked` / `pending` |
| `stop_reason` | string | 否 | 停止原因，仅 `status=stopped` 或 `blocked` 时必填 |

## 优化验证口径

- 所有优化按串行叠加方式验证，每轮基于前一轮已应用配置继续执行
- 每轮必须同时记录阶段增量收益和累计收益
- 报告默认采用串行叠加收益口径；只有用户明确要求时，才额外回到同一基线测单项独立收益
- 不得把多个并发变更后的混合结果拆分为单项收益
- 每轮必须记录分析耗时、实施耗时、验证耗时和总耗时，以及各优化分析项耗时明细
- 每轮必须形成明确的继续/停止决策，停止时必须记录停止原因

## cpu-affinity-optimization 执行门控

cpu-affinity-optimization 必须在所有其他候选 skill 之前执行：

- 进入任何其他候选 skill 的执行轮次前，必须确认 `cpu-affinity-optimization` 的 `per_skill_iteration_state.status` 为 `completed` 或 `stopped`。
- 确认方式：主 agent 必须从 `workflow_state.json` 读取 `candidate_skill_list` 中 cpu-affinity 条目的状态，或从 `per_skill_iteration_state` 读取其状态，并调用 `DynamicWorkflowManager.validateCpuAffinityFirst()` 进行验证。
- 若 cpu-affinity 尚未完成，主 agent 禁止加载其他 skill 的执行验证 subagent，禁止生成其他 skill 的执行任务包，禁止进入其他 skill 的迭代轮次。
- 违规执行视为合规失败，最终报告的 `skill_execution_order.cpu_affinity_first_verified` 必须为 `true`。

## 单 Skill 停止规则

架构图要求停止粒度是单个 skill，而不是整个 Agent。

单个 skill 满足以下任一条件时停止该 skill：

- 该 skill 已尝试最多 5 轮。
- 该 skill 已尝试 5 轮，且 5 轮阶段收益均 `< 1%`。
- 该 skill 的 high/medium 候选动作全部已验证、拒绝或因安全门禁暂缓。
- 该 skill 所需证据缺失且补采后仍无法形成可验证动作。
- 该 skill 的候选动作全部需要用户未批准的权限、重启、重编译、远程执行或硬件变更。

停止单个 skill 后：

- 必须继续执行 `candidate_skill_list` 中下一个未完成的 skill，包括 coverage 阶段 skill。
- 若所有主优化 skill 都已完成、停止或阻塞并说明原因，输出最终报告。
- 若瓶颈重新分类，必须重新采集或确认 `performance_signal_summary.json`，生成新的 `candidate_skill_list`，不得沿用旧候选列表盲目继续。

`per_skill_iteration_state` 至少记录：

```json
{
  "application-config-optimization": {
    "rounds_attempted": 5,
    "round_gains_pct": [0.6, 0.4, 0.2, 0.0, -0.1],
    "status": "stopped",
    "stop_reason": "five_rounds_gain_below_1_percent",
    "next_candidate_skill": "performance-library-selection"
  }
}
```

## 全局停止规则

全局停止只在以下场景触发：

- `bottleneck_classification=no_active_bottleneck`。
- 瓶颈不可识别，补采后仍为 `unknown_bottleneck`。
- 所有主优化 skill 均已完成、停止或阻塞并说明原因。
- 剩余动作均超出用户批准范围，且没有安全的 dry-run 或人工建议可继续验证。
- 用户要求停止或只输出报告。

## 候选二进制与源码补丁验证口径

当候选动作涉及源码补丁、重编译、替换二进制、替换动态库或运行时注入项时，必须额外执行以下门控：

- 实施前记录当前可执行文件、启动参数、配置文件、动态库、cpuset、NUMA 绑定和回退命令。
- 候选二进制必须先通过版本、健康检查、连接 smoke test、错误日志检查和目标实例身份校验，再进入正式压测。
- 若候选二进制和上一轮已采纳配置不是同一源码基线、同一配置、同一运行库和同一资源约束，只能标记为 `confounded_binary_test`，不得把结果归因为单个补丁。
- 正式验证前应先跑短 warmup。若 warmup 已明显回退、报错或目标实例身份不一致，应立即回退，不进入正式长测。
- 正式压测结果相对上一轮已采纳配置下降超过噪声阈值（默认 `> 2%`）时，必须回退并记录到 `rejected_optimization_actions`。
- 如果代码生成或反汇编验证成功，但 perf 热点和业务指标不匹配，应记录 `codegen_success_but_workload_mismatch=true`，停止围绕该补丁继续叠加优化。
