# 配对记录与复算工具

测量步骤、四段公式、自对照判定和结果解释见 [SKILL.md](../SKILL.md)。本文说明 `scripts/analyze_pairs.py` 接受的数据及输出，供接入项目采集器时使用。

## 一份文件对应什么

一份 JSON 只包含同一实验身份、同一参数点、同一独立进程的 AA、BB、AB。不同位宽、长度或进程分别生成文件和分析结果。

```text
顶层字段          类型       含义
experiment_id     非空字符串  关联源码、构建、机器、输入和协议的实验身份
workload_id       非空字符串  类型、位宽、长度、对齐、分布等参数点
process_run_id    非空字符串  独立进程身份
controls          对象       采集前确定的自对照门槛
expected_blocks   对象       计划组数，完整采集应提供
blocks            数组       原始四段记录，三类实验均须存在
```

`controls` 的四个字段均为有限正数，并满足以下顺序。实例门槛见主文；不能在看完结果后更改。

```text
p10_min <= median_min <= 1 <= median_max <= p90_max
```

`expected_blocks` 必须恰好包含 AA、BB、AB 三个键，值为正整数。例如 `{"AA": 21, "BB": 21, "AB": 21}`。实际组数多于或少于声明均拒绝。旧记录未声明时仍可复算，但完整性输出为 `not_declared`，需另查原计划。

## 四段记录

每个 block 保存以下字段。

```text
字段         类型       含义
experiment   字符串     AA、BB或AB
block_id     非负整数   同一实验类型内唯一的组编号
order        字符串     ABBA或BAAB
legs         数组       按时间顺序排列的四段记录
```

每个 leg 保存以下字段。

```text
字段             类型       含义
leg_index        整数       依次为1、2、3、4
label            字符串     与order对应位置一致的A或B
implementation   字符串     实际调用的original或candidate入口
iterations       正整数     本段逻辑调用次数N
elapsed_ns       正整数     本段原始纳秒耗时
```

AA 的两种标签都调用 original，BB 都调用 candidate，AB 的 A 调用 original、B 调用 candidate。同一输入文件的全部段共享 N；布尔值不能代替整数。工具拒绝超出浮点表达范围的比值，包括下溢为零的结果。

候选入口可能内部回退，所以 `implementation=candidate` 不代表 NEON 等优化内核已经执行。实际路径及回退原因作为额外元数据或审计记录保存，当前工具不校验它们。

身份在顶层声明。block 或 leg 也可带 `experiment_id`、`workload_id`、`process_run_id`，提供时必须与顶层一致。同一身份字符串只能帮助核对记录，不能证明实际使用了同一个构建或输入。

采集归档还应保存正确性记录、输入摘要、缓冲区复用和复位方式、实际处理值数、时钟、预热、命中证据、运行顺序、环境采样及退出码。这些可作为额外字段或独立文件关联，当前复算工具不自动验证。

## 运行与输出

从本 skill 目录运行，脚本只依赖 Python 标准库。下面的 tier A 用例使用 Python 3.9 或更高版本。

```sh
python3 scripts/analyze_pairs.py measurement.json > comparison.json
python3 cases/protocol-contract/run.py
```

输出保留三项身份和每类实验的 `blocks`、`order_counts`、全部 `ratios`、`median`、`p10`、`p90`。分位数采用排序后位置 `(n-1) × p` 的线性插值。

```text
状态字段              值               含义
measurement_status    controls_passed  自对照统计满足门槛，三类实验的顺序组数均平衡
measurement_status    inconclusive     自对照或顺序组数平衡未通过，AB比值仍保留
completeness_status   complete         组数与expected_blocks一致
completeness_status   not_declared     未声明组数，完整性需要另查
```

非法输入退出码为 2，成功复算为 0；输出 `inconclusive` 也属于成功复算。调用方应同时检查退出码和 JSON 状态。重定向可能已经创建空输出文件，不能仅凭文件存在判断成功。

每类实验的 ABBA 与 BAAB 数量之差不得大于 1。`inconclusive_reasons` 列出 `self_controls_failed`、`unbalanced_orders` 中适用的原因，通过时为空数组。顺序组数平衡不证明实际逐组交替；工具不校验原始时间顺序，也不要求文件中按时间排列各组。

工具不检查机器码、真实缓冲区、状态复位或候选命中，也不推断统计显著性。tier A 用例验证计算和拒绝条件；它不能替代目标机器上的测量或代理完整执行评估。
