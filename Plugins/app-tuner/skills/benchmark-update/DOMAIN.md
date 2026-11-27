# Benchmark 波动归因域

本组提供 Linux benchmark 多轮波动的只读归因能力。三个 skill 消费同一份
`benchmark-comparison/v1` 快慢样本，分别判断波动来自工作量变化、CPU 执行效率变化，
还是关键路径等待增加。

## 成员

| Skill | 职责 |
|---|---|
| [`benchmark-workload-variance`](benchmark-workload-variance/SKILL.md) | 判断慢样本是否执行了更多或不同的有效工作，包括输入混合、重试和执行路径变化 |
| [`benchmark-cpu-efficiency-variance`](benchmark-cpu-efficiency-variance/SKILL.md) | 在工作量与单位工作量指令数可比时，判断 CPI 上升或有效频率下降等 CPU 执行效率变化 |
| [`benchmark-wait-variance`](benchmark-wait-variance/SKILL.md) | 在工作量与 CPU 成本可比时，判断调度、节流、锁、I/O 或下游等待是否增加 |

## 域内路由

1. 尚未证明每轮工作量相同，或怀疑输入、重试、回源、GC/fallback 工作变化
   → `benchmark-workload-variance`。
2. 工作量与单位工作量指令数已可比，单位工作量 `task-clock` 增加
   → `benchmark-cpu-efficiency-variance`。
3. 工作量与 CPU 执行成本已可比，但墙钟时间仍增加
   → `benchmark-wait-variance`。

三个入口各自只交付一个独立判定，不自动修改系统配置或启动现场采样。上游流程应保留三个
decision 的证据与缺项，不得把 `inconclusive` 或 `blocked` 改写成确定归因。

## 输入与输出

- 共同输入：`benchmark-comparison/v1`。
- 工作量输出：`benchmark-workload-variance-decision/v1`。
- CPU 效率输出：`benchmark-cpu-efficiency-variance-decision/v1`。
- 等待输出：`benchmark-wait-variance-decision/v1`。

当前仓库内没有生成 `benchmark-comparison/v1` 的 producer；该契约是调用方与本组 skill 的外部交接面。
调用方可由现有 benchmark harness、采集归档或手工导出的 JSON 提供输入，但必须按各 skill 的
`references/contract.md` 明确工作量口径、预声明阈值、计数范围和计数质量。缺少这些声明时返回
`blocked` 或 `inconclusive`，不能把默认值当作已验证证据。后续接入 producer 时应直接产出同一
版本化契约，不另建隐式字段映射。

阈值必须在查看结果前由实验计划声明。固定时长压测中的完成请求数属于结果变量，不能直接用来
确认工作量原因；多线程程序的全进程 `task-clock` 也不能直接从 elapsed 中相减得到等待时间。

## 准入判据

新增成员须满足：

- 只负责一个可独立验收的 benchmark 归因或证据节点；
- 使用版本化输入输出契约，明确 `confirmed`、`not_supported`、`inconclusive` 和 `blocked`；
- 至少包含一个确认路径和一个容易误判的反例；
- 现场采集、系统配置变更和 A/B 干预需要单独授权，不由离线判定脚本隐式执行。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/app-tuner/skills/benchmark-update/**`
