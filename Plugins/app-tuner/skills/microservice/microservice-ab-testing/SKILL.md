---
name: microservice-ab-testing
description: 当需要对配置、OS 参数、库替换或运行时补丁做性能验证，并需要固定负载、预热、指标采集、正确性检查、收益判定和回退归档时使用。输出单变量轮次、判定结果和回退证据。
---

# Microservice A/B Testing

本 skill 只定义“怎么测、怎么判、怎么回退”，不定义具体组件改什么。配置类、库替换类和运行时补丁类 skill 负责提供自己的变更命令、组件指标和正确性用例；本 skill 负责把它们组织成可归因的单变量 A/B。

## 何时触发

- 任一微服务优化动作需要证明收益，而不是只给建议。
- 需要固定客户端、并发、请求分布、预热和时长，避免环境漂移。
- 需要单变量对比、正确性门禁、内存/错误率阈值和回退条件。
- 需要把多轮结果归档，供后续 review 或复现。

## 与组件 skill 的分工

组件 skill 必须提供：

- `forward_cmd` 与可独立执行的 `reverse_cmd`；
- 该组件的关键指标（例如 QPS/P99、RSS、GC、连接数、编解码吞吐）；
- 正确性用例或 smoke test；
- 该组件特有的停止/拒绝条件。

本 skill 提供：

- 基线、变更轮、组合轮的测量协议；
- 单变量和同负载约束；
- 收益判定、噪声区处理和回退规则；
- 归档字段。

## 必读 Reference

- `references/ab-testing-playbook.md` — 基线准备、轮次分组、指标采集、正确性、判定和归档的详细步骤。

## 输入

- 当前 `current_run_id`、基线指标、压测命令和目标实例身份。
- 组件 skill 给出的 `forward_cmd`、`reverse_cmd`、组件指标和正确性命令。
- 资源约束：CPU/内存配额、容器 limit、允许的重启/重编译范围。

## Workflow

### Step 0：基线

- 固定压测客户端、并发、请求分布、超时和重试策略。
- 固定预热与测量时长，预热和测量分开记录。
- 基线连续跑两轮，确认波动范围；波动过大时先修环境，不进入变更轮。
- 记录基线 QPS、P50/P99、错误率、RSS、CPU 和组件指标。

### Step 1：单变量变更

- 每轮只改变一个组件的一个变量；组件内部有多个绑定动作时，作为一个整体记录，但不得跨组件合并。
- 每个单变量轮次必须从同一基线启动：上一轮有变更被保留时，下一轮前恢复并核验 R0 基线，或从同一基线启动独立实例；否则标记 `confounded`。
- 变更前记录 `forward_cmd` 和 `reverse_cmd`。
- 变更后先做正确性检查，再做性能测量。

### Step 2：指标与判定

- 记录吞吐、延迟、错误率、RSS/内存水位和组件扩展指标。
- 正向收益且资源水位可控：保留。
- 负收益、错误率上升或资源超阈值：回退。
- 0%–1% 噪声区：标记 `inconclusive`，不重复归因。
- 组合轮只用于确认交互，不用于单独归因。

### Step 3：回退与归档

- 负收益或超阈值时立即执行 `reverse_cmd`，恢复上一轮有效配置。
- 归档压测原始输出、环境快照、组件 profile、`forward_cmd`/`reverse_cmd` 和轮次结论；环境快照只保留需要比较的配置键，秘密配置只记录是否变化，不落原值。

## 分支、终止与交回

- 基线波动超过约定范围：先修环境或延长预热/测量，不进入变更轮；无法收敛时 `status=blocked_baseline`，交回波动数据和已排除的环境项。
- 组件 skill 未提供 `forward_cmd`/`reverse_cmd` 或正确性用例：`status=blocked_dependency`，交回缺失项，不执行变更。
- 正确性失败或资源超阈值：立即执行 `reverse_cmd`，终止本轮；交回 `rollback_result`、失败证据和停止原因。
- 收益落在 0%–1% 噪声区：标记 `inconclusive`，保留原始数据，不重复归因。
- 至少 3 组有效对照（或按组件预先约定）且收益超过噪声线、正确性和资源均通过：判定 `accepted`。
- 正常完成：交回 `baseline_metrics`、`rounds[]`、`component_metrics`、`correctness_evidence`、`rollback_result` 和 `stop_reason`。

## 输出字段

- `baseline_metrics`
- `rounds[]`：每轮的变更、`before_metrics`、`after_metrics`、`stage_gain_pct`、`status`（accepted/rejected/inconclusive）
- `component_metrics`
- `correctness_evidence`
- `rollback_result`
- `stop_reason`

## 拒绝条件

- 无法固定压测负载或无法区分噪声。
- 组件 skill 未提供 `forward_cmd`/`reverse_cmd` 或正确性用例。
- 变更同时跨多个组件，无法做单变量归因。
- 资源水位没有阈值，或无法观察 RSS/内存。

## 边界

- 不提供具体组件的配置、补丁或代码修改命令。
- 不替代组件 skill 的正确性门禁；只负责测量协议与判定。
- 不记录或引用其它环境的历史性能数据；收益必须来自本轮 A/B。
