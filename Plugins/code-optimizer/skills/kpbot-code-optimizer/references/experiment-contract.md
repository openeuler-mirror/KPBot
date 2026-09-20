# 诊断实验记录契约

此契约定义 code-optimizer 的 `schema_version: 1` 实验记录。记录把实验设计、执行条件和原始证据引用保存在一起，但不根据元数据自动推断假设成立。

## 生命周期与保存位置

执行前设计可以使用相同结构且令 `evidence` 为空，`validate_record` 会接受这种设计，但它必须保存在编排器管理的独立草稿位置。`experiment_record.py` 的 `write_record` 和 CLI 会拒绝空证据，只写最终的实验尝试：

```text
RUN/experiments/<experiment_id>/record.json
```

每个 `experiment_id` 只能写一次。目录已经存在时拒绝覆盖，包括前次写入中断而留下的不完整目录。最终尝试即使失败，也应引用其错误日志等原始证据；等待目标环境时仍保留为草稿或编排状态。创建实验目录后的序列化或写入失败会明确报告“不完整记录”及保留的目录路径；不完整记录不能用于恢复候选或采纳优化。两个实验可以引用相同的 `parent_state`，但必须使用不同的 `experiment_id`。

记录中的 `experiment.command` 只是数据；记录工具不会执行它。

## 字段

所有正文字符串都必须包含非空白字符。ID 只能包含 ASCII 字母、数字、下划线和连字符。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | integer | 必须为 `1`，布尔值无效 |
| `run_id` | string | 合法 ID |
| `point_id` | string | 合法 ID |
| `experiment_id` | string | 合法且不可复用的 ID |
| `hypothesis_id` | string | 合法 ID，应随假设版本变化 |
| `parent_state` | string | 实验所基于的实际源码状态；存在未提交修改时不能只记录 Git HEAD，应使用能覆盖这些修改的状态或内容摘要 |
| `execution_context.target` | string | 目标机器或型号的可核验身份 |
| `execution_context.build` | string | 实际使用的构建参数或构建身份 |
| `execution_context.workload` | string | 实际工作负载身份 |
| `hypothesis.claim` | string | 可被实验支持的主张 |
| `hypothesis.alternative` | string | 替代解释 |
| `hypothesis.support_if` | string | 执行前声明的支持判据 |
| `hypothesis.refute_if` | string | 执行前声明的反驳判据 |
| `experiment.command` | string array | 非空；要在目标环境执行的命令参数 |
| `experiment.controlled` | string array | 非空；保持不变的条件 |
| `experiment.changed` | string | 本实验唯一改变或观察的因素 |
| `experiment.timeout_seconds` | integer | 正整数，布尔值无效 |
| `evidence` | array | 设计校验时可以为空；`write_record` 和 CLI 保存最终尝试时必须非空；非空时使用下述证据项结构 |

每个证据项必须包含：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `path` | string | RUN 内已经存在的普通文件的相对路径；绝对路径、目录、缺失文件、`..` 与符号链接造成的越界均拒绝 |
| `scope` | string | 非空；描述证据覆盖的请求、进程、函数或其他明确范围 |
| `kind` | string | `observation` 或 `estimate` |
| `source` | string | `target`、`fixture` 或 `historical` |

## 证据边界

`observation` 表示保存了观察结果，`estimate` 表示理论或模型估算。二者不能互换。`source` 只说明来源：

- `target`：来自当前目标环境；是否满足判据仍由反馈阶段核验。
- `historical`：只有目标、构建、工作负载和证据范围等条件可核验并与当前实验可比时，才可以用于当前判断。
- `fixture`：只用于测试记录逻辑，永远不能作为目标性能实测或因果结论的证据。

采样缺失不能证明代码没有执行。存在一个证据文件也只证明记录引用有效，不证明假设获得支持或被反驳。判断 `supported`、`refuted`、`inconclusive` 或 `invalid` 属于后续反馈阶段。

## CLI

```bash
python3 scripts/experiment_record.py --record INPUT --run-dir RUN
```

`INPUT` 是 JSON 文件，其中引用的证据已经位于 `RUN` 内。成功时标准输出仅打印保存后的 `record.json` 路径。JSON、字段、路径、目录或写入错误会输出到标准错误并返回非零状态；若实验目录已经创建，错误会同时标明该目录保留的是不完整记录。

## 反馈校验与独立当前上下文

编排器在 `RUN/current_context.json` 独立维护下述对象；实验 Agent 只能核对它，不能用记录内容生成或覆盖它：

```json
{
  "run_id": "r1",
  "point_id": "p1",
  "parent_state": "<包含未提交修改的当前源码摘要>",
  "execution_context": {
    "target": "<目标身份>",
    "build": "<构建参数/身份>",
    "workload": "<输入、数据集、并发条件>"
  }
}
```

这四个顶层字段必须与记录逐项相等；execution_context 对象整体相等。缺少上下文或任一条件不匹配时 `target_checked` 为 false，不能进入 DecideOptimization。实际条件或父源码发生变化时，编排器必须先更新当前上下文。此文件不是从待校验记录导出的自证材料。

反馈阶段新增条件（Task 1 基础校验仍允许无这些字段的设计）：

- 执行过的记录增加 `experiment.exit_code`，必须为整数而非 bool。非零退出码优先将执行状态转为 failed，即使传入 completed/supported，也只路由到 blocked_execution。失败尝试同样需要保存原始错误日志。复用历史结果时填写被复用命令的原始退出码，未知时不能声明 completed。
- 可核验 observation 增加 `provenance` 对象，含 `parent_state`、`execution_context: {target, build, workload}` 和 `scope`；前两者必须与当前记录匹配，scope 必须等于证据项 scope。source 为 target 或 historical，kind 为 observation 才可计入 `evidence_checked`。
- historical 还必须有非空 `reuse_explanation`，逐项说明源码、目标、构建、输入和范围为何匹配。原始 run_id/experiment_id 等身份可作为额外来源字段保留，不要求历史 run_id 等于当前运行。不能核实历史条件时不要填写匹配元数据。fixture 和 estimate 永远不计入目标 observation。至少一条有效 observation 才设置 evidence_checked。

完整反馈 JSON 必须包含 `run_id`、`point_id`、`experiment_id`、`hypothesis_id`（均与记录相等）、`execution_status`（completed/failed/awaiting_target_evidence）、`hypothesis_verdict`（supported/refuted/inconclusive/invalid）、布尔 `evidence_checked`、布尔 `target_checked`、非空 `explanation`、非空 `record_path`。record_path 使用 RUN 内相对路径或绝对路径，必须解析到 --record 指定的同一文件。执行过的尝试必须位于 `RUN/experiments/<experiment_id>/record.json` 且 evidence 非空；awaiting_target_evidence 只可引用 RUN 内且 experiments 目录之外的独立草稿，必须为 inconclusive、evidence 为空且 experiment 中不存在 exit_code；即使当前上下文匹配，两个 checked 标志也始终为 false。不能将最终尝试重新标为等待目标。

```bash
python3 scripts/experiment_feedback.py --record RECORD --feedback FEEDBACK --run-dir RUN --remaining N
```

CLI 先调用 Task 1 的 validate_record，再校验身份、最终记录位置、退出码、独立上下文和 observation provenance；输入 checked 标志仅作类型检查，其值完全重算。成功输出一个 JSON 对象：原反馈字段、重算的 execution_status/evidence_checked/target_checked、next_stage 和 reason。缺少上下文不会放行，返回 blocked_evidence（failed/awaiting 状态优先）；文件/JSON/必填字段/非法枚举/预算/记录路径错误返回非零退出码并在 stderr 说明原因。

`choose_next(feedback: dict, remaining: int) -> str` 仅验证路由子集（status、verdict、两个布尔标志及非负整数预算），供已完成校验的调用方使用，不独立核验证据。路由顺序为 awaiting_target_evidence、failed → blocked_execution、任一 checked 为 false 或 invalid → blocked_evidence、supported → DecideOptimization、剩余额度为零 → stop_budget、refuted → AnalyzeHotspot、inconclusive → DesignExperiment。supported 即使剩余额度为零也可以进入决策。

这些检查验证引用与声明的来源条件，不验证文件内容是否真实，也不自动证明因果关系或抵抗恶意伪造元数据。Agent 必须阅读原始结果，引用证据并对照预先声明判据给出解释；完整项目验收仍是后续阶段职责。
