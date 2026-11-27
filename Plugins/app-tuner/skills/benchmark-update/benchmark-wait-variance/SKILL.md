---
name: benchmark-wait-variance
description: 当 Linux benchmark 的工作量和 CPU 执行成本已可比，需要判断波动是否由调度延迟、cgroup 节流、锁、I/O、缺页、GC 停顿或下游等待引起时使用。交付等待归因判定、等待类别、缺项和结构化 decision.json。
---

# Benchmark 等待时间波动识别

只判断关键路径是否增加了 runnable-but-not-running 或 blocked 时间。不要把额外工作或 CPU 执行成本增加归入等待。

## 适用场景

- 快慢样本的工作量、输入和单位工作量指令数可比。
- 墙钟时间变化大，但 CPU 执行成本不足以解释差距。
- 已有关键路径 CPU 时间，或单线程 benchmark 的 `task-clock`。

不适用于多线程程序只有全进程累计 `task-clock`、无法识别关键线程，或工作量仍未冻结的任务。

## 必需输入

提供 `benchmark-comparison/v1` JSON，至少包含：

- fast、slow 两组样本及预声明阈值；
- `elapsed_ms`、有效工作量、`instructions`；
- 单线程的 `task_clock_ms`，或多线程的 `critical_path_cpu_ms`；
- 可选的 `runnable_delay_ms`、`blocked_ms`、`throttled_ms`、`io_wait_ms`、`downstream_wait_ms`。

详细字段和输出语义见 [输入输出契约](references/contract.md)。cgroup、PSI 和系统级计数必须使用 benchmark 窗口内增量；系统全局压力只能作为背景，不能单独证明本进程在等待。

`benchmark-comparison/v1` 当前由调用方或外部采集流程提供，仓库内尚无 producer。输入必须保留来源
和窗口；多线程样本没有 `critical_path_cpu_ms` 时交回 `blocked`。

## 依赖与权限

- Python 3.9 或更高版本，仅使用标准库。
- 本 skill 不主动运行调度追踪、`strace`、eBPF、I/O 采样或下游请求。
- 不修改 CPU quota、绑核、存储或服务配置。此类 A/B 动作必须由上游获得单独授权并负责恢复。
- 输出只能写在当前工作目录内，父路径不得包含符号链接。默认不覆盖已有输出，显式 `--force` 也只能替换同一 detector、run_id 和 contract 的已有 decision。

## 用法

在本次 benchmark 的专用运行目录执行；脚本路径可使用 skill 安装位置的绝对路径：

```bash
python3 scripts/classify.py --input comparison.json --output decision.json
```

## 判断方法

1. 核对有效工作量和单位工作量指令数等价。
2. 选取关键路径 CPU 时间。单线程可使用 `task_clock_ms`；多线程必须提供 `critical_path_cpu_ms`。
3. 对每个样本计算 `non_running_ms = elapsed_ms - critical_path_cpu_ms`。
4. 慢组墙钟和 non-running 时间都超过实质变化线、关键路径 CPU 成本仍在等价线内，且新增 non-running 时间解释了预声明比例的墙钟差，输出 `confirmed`。
5. 若 CPU 成本足以解释慢轮，输出 `not_supported`。缺关键路径或时间分解时输出 `blocked/inconclusive`。

若提供等待细分计数，脚本按 fast/slow 中位数增量选择贡献最大的等待类别；没有细分时可以确认“等待增加”，但 `wait_class` 保持 `unattributed`，不能凭经验猜测锁、I/O 或调度。

## 产物与交回

输出 `benchmark-wait-variance-decision/v1` JSON，包含状态、置信度、墙钟/运行/non-running 分解、解释比例、等待类别、证据和缺项。

给人看的报告必须和 decision JSON 使用同一批样本。受阻时交回关键路径缺项和仍可用的描述性指标，不自动补采。

关键路径 CPU 成本必须在等价线的正负两侧都成立。CPU 成本上升应转到 CPU 效率分析；CPU 成本显著
下降而 non-running 时间增加属于混合变化，返回 `inconclusive`，不能因解释比例超过 100% 而确认等待。

## 域内衔接

- 工作量或单位工作量指令数不等价时，回到 [工作量识别](../benchmark-workload-variance/SKILL.md)。
- 关键路径 CPU 成本不等价时，转到 [CPU 执行效率识别](../benchmark-cpu-efficiency-variance/SKILL.md)。
- 三者都可比且等待确认后，交回等待类别和缺项，不在本 skill 内自动改调度、cgroup 或服务配置。

## 验证

```bash
python3 cases/classification-contract/run.py
```

用例覆盖 blocked 时间增长的确认路径，以及“多线程程序只有聚合 task-clock，不能直接用 elapsed 减 task-clock”的反例。
