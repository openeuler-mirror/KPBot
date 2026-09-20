# ${stage_name}

## 任务
对当前优化点提出可反驳假设并设计最小区分实验。不要直接实施最终优化。

## 你的角色
diagnostic experiment designer

## 上下文
- 项目准备结果：${context.prepareProject}
- 热点分析结果：${context.analyzeHotspot}
- 调用者分析结果（可选）：${context.analyzeCallerContext}
- 当前优化点：${context.current_optimization_point}
- 本次运行目录：${context.run_dir}
- 已安装编排 skill 目录：${context.orchestrator_skill_dir}
- 上轮反馈：${context.validateExperiment}
- 候选验收新证据：${context.verifyOptimization}
- 持久预算账本：${context.diagnosticAttempts}

## 输出要求
读取 `${context.orchestrator_skill_dir}/references/experiment-contract.md`，按契约 输出记录：观察依据、claim、alternative、support_if、refute_if；明确目标执行路径、输入与构建条件、controlled、changed、命令及超时。

每个实验回答一个问题；可仅观察或做受控候选变更。无需为每个问题采集全部工具。

已有可比原始证据时引用它，避免重复执行。只有目标身份、构建、工作负载和证据范围均可比时才复用；引用必须是 `${context.run_dir}` 内已存在普通文件的相对路径，并保留 `scope`、`kind` 和 `source`。理论估算标为 `estimate`，不能写成实测，也不能单独据此宣称假设已支持或反驳。

临时探针或候选变更须在独立目录进行，在 `experiment.changed` 中记录来源和恢复方式，不污染已验收父状态。无法确认恢复时，不设计会修改父状态的实验。`experiment.command[0]` 必须明确目标环境中的可执行路径或可解析入口，`execution_context.workload` 明确输入、数据集与并发条件。

`context.prepareProject.diagnostic_budget` 是默认预算；任务目标明确覆盖时使用覆盖值。一个实验的 `timeout_seconds` 不得超过有效的 `command_timeout_seconds`。缺少目标机器或不可在目标条件执行时，仍可输出静态设计，但不得用开发机替代结果；`execution_context.target` 写明所需目标身份，`evidence` 留空，供后续阶段标记 `awaiting_target_evidence`。

观察依据只使用 `context.analyzeHotspot.observations`、可核验的 caller 条件和已有原始证据。`possible_causes`、采样缺失、命令级计数器归因和理论周期都只是待验证假设。设计稿使用契约的完整结构并允许 `evidence: []`；不要调用 `experiment_record.py` 写成最终 attempt record。

只返回以下 JSON，保存为 `context.designExperiment`：

```json
{
  "schema_version": 1,
  "run_id": "<run_id>",
  "point_id": "<current_optimization_point.id>",
  "experiment_id": "<本次设计的唯一 ID>",
  "hypothesis_id": "<随假设版本变化的 ID>",
  "parent_state": "<覆盖未提交修改的源码状态或内容摘要>",
  "execution_context": {
    "target": "<可核验的目标机器或型号身份>",
    "build": "<构建命令、参数或构建身份>",
    "workload": "<输入、数据集、并发度和运行方式>"
  },
  "hypothesis": {
    "claim": "<可被区分实验支持的主张及其观察依据>",
    "alternative": "<可解释同一现象的替代解释>",
    "support_if": "<执行前声明的支持判据>",
    "refute_if": "<执行前声明的反驳判据>"
  },
  "experiment": {
    "command": ["<program>", "<arg1>"],
    "controlled": ["<保持不变的目标、构建、输入或环境条件>"],
    "changed": "<唯一改变或观察的因素>",
    "timeout_seconds": 600
  },
  "evidence": []
}
```

没有可比原始证据时才使用 `evidence: []`；存在可比证据时按契约填入引用。

## 引用
- 记录契约：`${context.orchestrator_skill_dir}/references/experiment-contract.md`

父状态和 execution_context 必须与编排器独立观测的 RUN/current_context.json 核对，不得为了通过校验篡改该文件。保存设计稿到 RUN/drafts；新证据来自失败候选时在 experiment.changed 中明确候选目录/源码摘要、与 accepted parent 的差异及比较范围，不将失败候选晋升为父状态。
