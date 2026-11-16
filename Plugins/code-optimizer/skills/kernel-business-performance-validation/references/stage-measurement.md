# 会话区间记录与工具

业务验证步骤、计时范围、并发归属、去重规则及结论判定见 [SKILL.md](../SKILL.md)。本文说明 `scripts/analyze_session.py` 的单会话输入和输出。

## 输入字段

一份 JSON 对应一次已结束的业务样本。

```text
顶层字段       类型         含义
session_id     非空字符串   本次请求或查询的唯一会话身份
clock_id       非空字符串   本文件内可比较的时钟身份
start_ns       非负整数     业务计时开始时刻
end_ns         非负整数     业务计时结束时刻，必须晚于开始
stopped        布尔值       必须为true
active_spans   整数         必须为0
spans          数组         需要分析的目标阶段区间
```

每个 span 保存以下字段。

```text
字段          类型         含义
span_id       非空字符串   文件内唯一的区间编号
session_id    字符串       与顶层会话一致
clock_id      字符串       与顶层时钟一致
stage         非空字符串   阶段名称，如bp_full或rle_expand
start_ns      非负整数     区间开始时刻
end_ns        非负整数     区间结束时刻，必须晚于开始
```

全部区间必须完整落在业务窗口内。跨线程记录使用同一个可比较时钟；同名 `clock_id` 本身不能证明时钟已同步。只输入需要分析的目标阶段，不要把整个查询也当作目标区间，否则并集比例会变成 100%。

工具没有“完整解码阶段白名单”，所有输入区间都会进入合计并集。计算 BP/RLE 完整合计时，仅选两类完整区间；内层阶段可在另一份分析输入中复算。保留筛选前原始记录和筛选规则，未采到完整阶段时不把内层并集改名为完整合计。

构建、算法模式、统计状态、SQL、数据、轮次、调用次数、实际工作量、正确性、命中和采集器错误等元数据应随样本保存或关联。当前工具不使用这些额外字段完成跨模式分析。

## 输出口径

```text
输出字段                       含义
query_ns                       业务窗口长度
stage_ns                       每种stage的累计墙钟时间
stage_overlap                  每种stage内部是否存在区间重叠
stage_fraction                 无同stage重叠时的累计时间/业务窗口，否则为null
combined_wall_union_ns         所有输入目标区间的墙钟并集长度
combined_wall_union_fraction   并集长度/业务窗口；无区间时为null
fraction_kind                  固定为wall_interval_union，标明合计比例口径
overlap_observed               所有输入区间之间是否存在重叠
capture_status                 captured或no_target_spans
```

`overlap_observed` 包含父子嵌套及并发，工具不能区分两者。`stage_ns` 可能超过查询窗口；并集长度不会超过窗口，但它只表示存在目标活动的时间，不表示 CPU 时间、串行贡献或关键路径贡献。

同一阶段若无内部重叠，`stage_fraction` 可给出活动时间比例；不同阶段仍可能互相嵌套，不能直接相加这些比例。父子及并行示例见主文。

空 `spans` 是可解析输入：输出 `no_target_spans`，并集长度为零、合计占比为 null。需要另查候选未命中还是没有采到记录。`captured` 也只表示文件有有效区间，不能证明采集完整或候选实际执行。

## 运行与退出码

从本 skill 目录运行，脚本只依赖 Python 标准库。tier A 用例使用 Python 3.9 或更高版本。

```sh
python3 scripts/analyze_session.py session.json > session-summary.json
python3 cases/protocol-contract/run.py
```

用例结果写入 `cases/protocol-contract/results/result.json`。`case.yaml` 的入口和断言路径均相对用例目录解析，不相对插件根或 skill 根解析。

非法输入退出码为 2，有效输入为 0；空区间也返回 0，需同时检查 `capture_status`。重定向产生的空文件不能当作成功结果。

## 采集器仍需保证什么

工具复算已有区间，不安装服务端计时器，也不管理线程、任务或请求生命周期。`stopped=true`、`active_spans=0` 是输入声明，必须由采集器如实提供。工具无法发现漏报的区间、错误贴上的会话标识或未记录的晚到任务。

只有阶段累计量、没有起止区间的历史数据，无法恢复并行重叠的并集。多模式、统计开关、轮次汇总以及正确性和命中审计依照主文执行；tier A 测试只验证离线计算与格式拒绝条件。
