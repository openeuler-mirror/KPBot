# 数据与证据约定

## 运行目录

建议一个独立运行使用如下布局：

```text
run-unique-id/
  collection-01/
    stdout.txt
    stderr.txt
    manifest.json
  topdown-summary.json
  context.json
  findings.json
  01-Profiling.md
  02-优化建议.md
```

采集器只生成 collection 目录里的三个文件。
analyze 生成 summary；context 和 findings 由执行诊断的 Agent 编写。
没有实现自动根因推断或自动优化动作生成器。
不要把示例目录当成实际存在的产物。

## manifest.json

| 字段 | 含义 |
| --- | --- |
| command | 实际执行的程序及参数数组 |
| target | pid 或 cpus，保留输入范围 |
| scope | user / kernel / all |
| duration_seconds | 请求的采集时长 |
| profile | DevKit profile 选择 |
| started_at / ended_at | UTC ISO 时间，包含工具启动开销 |
| exit_code / signal | 工具进程结果 |
| timed_out | 是否触发采集器时限 |
| status | collected / failed / timeout |
| files | stdout 与 stderr 的 SHA-256 |
| context | 可选的人类描述，不替代环境记录 |

collected 仅表示工具以零退出，不保证数据有效。
实际采样窗口可能短于进程运行区间，优先使用原始报告窗口。
manifest 哈希用于一致性检查，不是来源真实性认证。

## context.json

按已知事实记录以下内容，未知项用 null：

- run_id、主机标识和 CPU 型号。
- DevKit 版本以及采集说明来源。
- 目标服务身份、PID、线程或 cgroup 边界。
- 负载输入、并发、阶段及是否稳定。
- 业务目标、同窗业务指标及单位。
- 已知环境差异、采样干扰、权限限制。

环境信息不自动由脚本完整采集，必须主动核对。
不要将本机离线解析环境写成目标服务器环境。

## topdown-summary.json

schema_version 当前为 1.0。
source 包含原文件绝对路径、字节数和 SHA-256。
交付时使用报告内的相对证据链接，不依赖绝对路径可移植性。
blocks 按原文出现顺序保存，不假设最后一个就是总报告。
每个块保留 title、start_line、context_lines、counters、metrics 和 pmu_events。
context_lines 可能含窗口说明，但脚本不保证能解释其格式。

每个 metric 保存：

| 字段 | 含义 |
| --- | --- |
| name | 工具显示名称 |
| value | 数值或 null |
| unit | percent，仅针对 Bound(%) 表 |
| denominator | 默认 tool-defined/unverified |
| tree_prefix | 原始树形缩进，供复核父子关系 |
| line | 原文件一基行号 |
| raw | 完整原始行 |

脚本不根据空格推断 parent_id，防止不同版本缩进造成错误树。
报告可按原始前缀展示树；分析者确认父子关系后再解释。
PMU count 用十进制字符串保存，避免大整数精度损失。
Cycles/Instructions 的 raw 字段保留原值；数值字段仅用于粗略验收。
超过安全整数范围时，精确计算请使用 raw 字符串与大整数处理。

## 解析状态

| 状态 | 意义 | 后续动作 |
| --- | --- | --- |
| ready_for_review | 识别到指标且未触发格式警告 | 继续核对来源和指标语义 |
| needs_review | 有可解析数据但存在缺口或异常 | 逐项复核，不跳过警告 |
| unusable | 没有可用指标行 | 人工检查或补充原始文件 |

专项 profile 可能没有四大类，因此会触发 root_count。
这不是宣称工具错误，而是提醒不能作为完整 Level1 解读。
缺测、负值、超范围、重复根指标、总和异常均保留并告警。
未知表格格式不自动兼容，原始文本是最终复核依据。
stderr 不由 analyze 自动解读，交付前必须读取。

## findings.json

建议使用以下顶层字段，便于交给报告整合者：

```json
{
  "source_skill": "topdown-analysis-advisor",
  "run_id": "actual-run-id",
  "findings": [],
  "candidate_actions": [],
  "limitations": []
}
```

finding 条目包含 finding_id、observation、interpretation、confidence、evidence_sources。
confidence 使用 high / medium / low，并附解释，不自动按阈值赋值。
evidence_sources 写实际路径、block_id、line 和必要的采样范围。
candidate_action 包含 action_id、finding_ids、proposal、preconditions、experiment、branches、status。
建议可独立存在于 Markdown；使用 JSON 时字段不得与正文相互矛盾。
这些是本专项约定，不宣称主流程现有脚本已自动支持该结构。

## 不兼容情况

- perf stat 的 TopdownL1 格式：人工解读，不喂给 DevKit 专用解析器冒充结果。
- 截图：先核对转录内容，标记转录来源，避免虚构原文件行号。
- 单纯 cache miss 摘要：作为旁证，不能填充四大类。
- 跨平台指标：分别记录定义，不直接计算差值。
