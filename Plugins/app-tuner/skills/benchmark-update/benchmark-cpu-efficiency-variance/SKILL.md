---
name: benchmark-cpu-efficiency-variance
description: 当 Linux benchmark 的工作量和单位工作量指令数已可比，需要判断波动是否由同样工作在 CPU 上执行更久引起时使用。交付 CPU 效率归因判定、硬件计数证据、缺项和结构化 decision.json。
---

# Benchmark CPU 执行效率波动识别

只判断等量工作是否因为 CPI、频率、缓存、内存、NUMA 或同核竞争等 CPU 执行效率变化而变慢。额外工作和 off-CPU 等待不属于本 skill 的确认范围。

## 适用场景

- 快慢样本的业务工作量、输入混合和代码版本一致。
- 已有 `task-clock`、`instructions`、`cycles`，需要判断慢轮是否在 CPU 上花了更多时间。
- 上游工作流需要一个可独立验收的 CPU 效率判定节点。

不适用于工作量或单位工作量指令数明显变化，或只有墙钟时间而没有 CPU 计数的任务。

## 必需输入

提供 `benchmark-comparison/v1` JSON，fast、slow 每组达到预声明最少样本数。每个样本至少包含：

- `elapsed_ms`、有效工作量；
- `task_clock_ms`、`instructions`、`cycles`；
- 显式的 `counter_quality`、`execution_model` 和 `counter_scope`；
- 同一 binary/config/phase 的条件标识；
- 采集前声明的实质变化线和等价线。

详细字段、公式和输出语义见 [输入输出契约](references/contract.md)。`perf stat` 计数发生 multiplex 且缩放质量未知时，应在输入中标记，返回 `inconclusive`，不能伪装成精确计数。

`benchmark-comparison/v1` 当前由调用方或外部采集流程提供，仓库内尚无 producer。缺少计数质量声明
时返回 `blocked`；多线程样本只有全进程或 cgroup 累计计数时返回 `inconclusive`，不能用总 CPU
时间推断关键线程变慢。

## 依赖与权限

- 离线判定脚本需要 Python 3.9 或更高版本，仅使用标准库。
- 本 skill 不主动运行 `perf`、绑核、调 governor、修改 cgroup 或 NUMA 策略。
- 允许的副作用只有在当前工作目录内写 decision 文件；父路径不得包含符号链接。默认拒绝覆盖，显式 `--force` 也只能替换同一 detector、run_id 和 contract 的已有 decision。

## 用法

在本次 benchmark 的专用运行目录执行；脚本路径可使用 skill 安装位置的绝对路径：

```bash
python3 scripts/classify.py --input comparison.json --output decision.json
```

若需要先采集，可由已获授权的上游流程使用项目现有工具生成输入。本 skill 只消费结果，不把分析授权扩大为现场采集或配置修改授权。

## 判断方法

1. 先检查有效工作量在等价线内。
2. 检查单位工作量指令数在等价线内；若明显增加，说明执行了更多代码，CPU 效率归因不成立。
3. 计算单位工作量 `task-clock`、CPI 和有效频率。CPI 为 `cycles / instructions`；有效频率为 `cycles / task_clock`。
4. 单位工作量 CPU 时间超过实质变化线，并且 CPI 上升或有效频率下降达到实质变化线，才输出 `confirmed`。
5. 若墙钟变慢但单位 CPU 时间不增加，输出 `not_supported`，将等待分析作为下一候选。CPU 时间增加但缺少可区分机制的计数时输出 `inconclusive`。

多线程程序的 `task-clock` 是线程 CPU 时间总和。输入必须说明此指标是否适合当前比较；若 benchmark 的完成时间由关键线程决定，而只有无边界的全进程累计值，应返回 `inconclusive`。

## 产物与交回

输出 `benchmark-cpu-efficiency-variance-decision/v1` JSON，包含状态、置信度、工作量/指令等价性、单位 CPU 时间、CPI、有效频率、证据和缺项。

人读结论必须引用同一份 decision JSON。若输入缺硬件计数、计数质量不足或条件漂移，保留已算出的描述性值并交回 `inconclusive/blocked`，不猜测具体微架构原因。

## 域内衔接

- 工作量或执行路径尚不可比时，先回到 [工作量识别](../benchmark-workload-variance/SKILL.md)。
- 单位工作量 CPU 时间没有增加但墙钟变慢时，转到 [等待时间识别](../benchmark-wait-variance/SKILL.md)。
- 多线程聚合计数不能确认 CPU 效率原因，先补关键路径计数再重跑本 skill。

## 验证

```bash
python3 cases/classification-contract/run.py
```

用例覆盖 CPI 上升、有效频率从 3.0 GHz 降到 2.4 GHz 的确认路径，以及“墙钟变慢但 CPU 计数稳定，应转入等待分析”的反例。
