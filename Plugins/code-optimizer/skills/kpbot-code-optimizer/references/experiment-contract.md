# 诊断实验记录契约

此契约定义 code-optimizer 的 `schema_version: 1` 实验记录。记录把实验设计、执行条件和原始证据引用保存在一起，但不根据元数据自动推断假设成立。

## 生命周期与保存位置

执行前设计可以使用相同结构且令 `evidence` 为空，但必须保存在编排器管理的独立草稿位置。`experiment_record.py` 的 `write_record` 和 CLI 只写最终的实验尝试：

```text
RUN/experiments/<experiment_id>/record.json
```

每个 `experiment_id` 只能写一次。目录已经存在时拒绝覆盖，包括前次写入中断而留下的不完整目录；不完整记录不能用于恢复候选或采纳优化。两个实验可以引用相同的 `parent_state`，但必须使用不同的 `experiment_id`。

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
| `evidence` | array | 可以为空；非空时使用下述证据项结构 |

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

`INPUT` 是 JSON 文件，其中引用的证据已经位于 `RUN` 内。成功时标准输出仅打印保存后的 `record.json` 路径。JSON、字段、路径、目录或写入错误会输出到标准错误并返回非零状态。
