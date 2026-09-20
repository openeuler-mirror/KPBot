# ${stage_name}

## 任务与上下文
验证诊断实验并形成反馈，不提交优化或修改默认 skill。

- 实验设计：${context.designExperiment}
- 项目准备：${context.prepareProject}
- 当前优化点：${context.current_optimization_point}
- 本次运行目录：${context.run_dir}
- 已安装编排 skill 目录：${context.orchestrator_skill_dir}
- 记录 helper 绝对路径：${context.record_script}
- 反馈 helper 绝对路径：${context.feedback_script}
- 持久预算账本：${context.diagnosticAttempts}
- 本次执行预留：${context.executionReservation}
- 候选验收证据（可复用）：${context.verifyOptimization}

## 执行与证据
1. 按 `${context.orchestrator_skill_dir}/references/experiment-contract.md` 核对编排器独立维护的 `RUN/current_context.json`：run_id、point_id、parent_state 和 execution_context 的目标、构建、工作负载必须与设计及实际环境一致。不要从实验记录复制内容来伪造当前上下文。未提交源码必须包含在父状态摘要中。
2. 目标环境不匹配或不可用，保留设计在 `RUN/drafts/<experiment_id>.json`，返回 `awaiting_target_evidence`；工具执行失败返回 `failed`，区分工具/环境故障与候选代码错误。不在当前开发机器测替代性能。诊断失败不直接进入 FixCode。
3. 新实验串行执行；执行前必须有编排器已落盘且 experiment_id/command/timeout 匹配的 executionReservation；缺失或已执行的 reservation 禁止启动命令，返回 blocked_execution 原因给协调者（未执行失败不能伪造成最终记录）。预算由编排器扣减，使用设计超时且不得超过有效 command_timeout_seconds。预算为零时不启动命令，记录 stop_budget。缺少目标不扣额度；复用已核对身份和条件的结果不扣新执行额度。无新证据或变更不原样重试。
4. 在目标环境执行已设计的命令，读取原始结果并记录实际条件与整数 `experiment.exit_code`，超时/工具错误也保存日志及非零退出状态。将原始文件保存到 `RUN/raw/<experiment_id>/`，不要预建最终 experiments 目录。执行命令是本阶段 Agent 的职责，记录/反馈脚本不会执行命令。
5. 每条证据保留 path、scope、kind、source。可用于目标核验的 observation 增加 `provenance: {parent_state, execution_context: {target, build, workload}, scope}`，全部来自实际执行条件。历史证据保留其原始身份，增加 `reuse_explanation`，明确核对父源码、目标、构建、工作负载和范围如何与当前记录匹配。fixture/estimate 不支持目标验证，历史条件不明时保持 inconclusive。
6. 逐项引用原始证据及 scope，对照执行前的 support_if/refute_if 作出 supported/refuted/inconclusive/invalid，并写 explanation。分别描述观察与解释；相关性只能用于提出假设，采样缺失不能证明未执行。checked 字段由 CLI 重算，脚本验证元数据和路由，不自动证明因果关系。

## 保存与反馈
执行过的尝试（含失败）用 `python3 "${context.record_script}" --record "ABS_INPUT" --run-dir "${context.run_dir}"` 保存不可变最终记录；必须引用原始输出/错误日志。等待目标仍为草稿，不能调用最终记录写入工具。准备 feedback JSON：

```json
{
  "run_id": "<record.run_id>",
  "point_id": "<record.point_id>",
  "experiment_id": "<record.experiment_id>",
  "hypothesis_id": "<record.hypothesis_id>",
  "execution_status": "completed",
  "hypothesis_verdict": "inconclusive",
  "evidence_checked": false,
  "target_checked": false,
  "explanation": "<判据、引用路径和范围、观察、解释及仍缺少的证据>",
  "record_path": "experiments/<experiment_id>/record.json"
}
```

等待目标使用 `execution_status: awaiting_target_evidence`、`hypothesis_verdict: inconclusive` 和 experiments 目录之外的实际草稿路径；草稿必须 evidence 为空且不存在 experiment.exit_code，两个 checked 标志保持 false。最终记录不得再次修改或改标为等待目标。

```bash
python3 "${context.feedback_script}" --record "ABS_RECORD" --feedback "ABS_FEEDBACK" --run-dir "${context.run_dir}" --remaining N
```

`N` 是执行扣减后的剩余额度，复用时保持原值。使用 CLI 的完整 JSON 输出保存为 `context.validateExperiment`，包括重算的 checked 标志、next_stage 和 reason。报错时保留错误并阻塞，不能自行填补成 supported。supported 进入 DecideOptimization；refuted 返回 AnalyzeHotspot；inconclusive 返回 DesignExperiment；后两者额度耗尽时 stop_budget；证据缺失/invalid 为 blocked_evidence；命令失败为 blocked_execution；等待目标为 awaiting_target_evidence。局部诊断有支持仍不能替代优化后的项目完整正确性与约定工作负载验收。

复用候选完整验收日志时，把原命令、原 exit_code、候选摘要和 accepted parent 对照关系写入新 ID 的记录，不改写既有最终记录。设计必须已声明该结果可检验的判据，否则返回协调者先 DesignExperiment。无目标证据只保留等待草稿；该路径不消耗新执行额度。最终文件必须使用记录 CLI 实际返回路径，不能把 INPUT 草稿当 --record；所有 CLI 文件参数均解析为 RUN 内绝对路径。
