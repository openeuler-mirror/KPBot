# 用真实 Ray 证据演练分析

输入为 `cases/real-ray/run.sh` 生成的 `results/real-ray/`，不是预填的性能数字。该用例交错执行 row-map 与 batch-map，各三轮，每轮对 256 个整数求平方，逐键精确核对。计时范围是 `ds.materialize()` 调用本身，stats 取自同一轮的 `measured = ds.materialize()` 后的 `measured.stats()`，不重跑。用例使用真实 Ray，但业务是合成 ETL，不代表 ImageNet、存储性能或模型推理。

## 读什么、能说什么

1. 读 environment.json、row.json、batch.json：确认版本、CPU/内存范围、输入身份、计时范围，以及唯一声明变量 config.mode。Ray 逻辑 CPU 不等于容器物理配额；配额隐藏或未知时只描述观测，不推断整机利用率。
2. 对照每轮 plan.txt 与 stats.txt：把逻辑 map/map_batches 对应到实际 operator，记录 fusion、block/task、输出行数、累计 CPU/墙钟时间及执行耗时。stats.txt 保存的是 `measured.stats()` 原文；若版本未提供公共 explain 接口，用例保留 Dataset 表示和真实 stats，不伪造物理计划。
3. 读 correctness.json：必须每轮有 256 个唯一 id 且 value=id²，并核对 `udf_calls_at_materialize` 与 `udf_calls_after_read` 相等，证明读取物化结果没有触发第二次执行。若仅行数相等而数值未核对，不视为正确性通过。
4. 读 comparison.json：核对三轮原始值、median、CV、显式变化因素和 comparable。观察到耗时差异不等于 batch 一定改善；此小负载的启动、调度与波动可能主导，不能宣布业务性能收益。

## 一次实际运行的读数

以下数值来自 2026-11-23 在一个 4 CPU `--cpus` 容器内执行本用例的真实输出（Ray 2.52.0、Python 3.11.14、`ray.init(num_cpus=2)`）；仅用来演示如何读证据，不是性能结论。environment.json 显示 `visible_cpu_upper_bound=4.0`，而 affinity 仍是宿主可见的 96 个 CPU，说明不能把 affinity 直接当成容器可用核数。

row 三轮 `materialize()` 耗时是 **0.479s、0.0437s、0.0436s**，batch 三轮为 **0.0663s、0.0655s、0.0646s**。comparison.json 的中位数是 row 0.0437s、batch 0.0655s，按低者优得到 `speedup=0.667`、`improvement_percent=-49.9`；row 的 CV 为 **1.331**，batch 的 CV 为 0.013。首轮偏慢是观测事实，但仅凭耗时和 CV 无法区分 worker 初始化与调度等待，二者只能作为待验证解释；需要与首轮计时窗口对齐的 worker 启动事件或时间线才能进一步判断。所有轮次 correctness.json 都通过，且读取物化结果没有增加 UDF 调用次数，只能说明两条路径结果一致；不能据此断言 row 比 batch 快，也不能把中位数差异归因于表达方式。合理的下一步是单独预热、固定调度和输入后交错重测，而不是直接删除 batch 路径。

## 建议交付报告

```text
目标：判断 row/batch 表达方式在当前合成 ETL 中的实测差异。
输入与范围：256 个确定性整数，构造 Dataset 到 materialize 完成；每种三轮。
正确性：引用六份 correctness.json，说明逐键核对而非仅计数。
观察：引用本轮 measured.stats() 记录真实 operator、任务数和时间；引用文件与原文。
描述性对比：引用 comparison.json 的原始样本、median/CV；不填预期收益。
归因状态：仅凭这些证据，无法断言 CPU、存储或格式转换是主因。
替代解释：短任务调度成本、首次 worker 初始化（与调度等待均待验证）、任务划分与计时波动。
下一步：固定输入规模与资源，按目标业务扩大代表性工作量；需归因时补 worker 栈或时间线。
限制：该用例无存储 I/O、无真实模型；不外推到集群或 ImageNet。
```

用户提供真实任务后沿用同一结构，把样例指标和假设替换成用户证据。原始统计缺失时交回缺项与最小采集建议，不能从“低 CPU”直接断定 I/O 瓶颈，也不能把 task 累计耗时相加作为流水线墙钟占比。
