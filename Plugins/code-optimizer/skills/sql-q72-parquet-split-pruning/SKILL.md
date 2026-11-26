---
name: sql-q72-parquet-split-pruning
description: 消费已验证的分区 Bloom 绑定，在 Gluten 提交普通 Hive Parquet split 前求值并剪枝。适用于分区值对整个 split 恒定，原生探针可明确返回 false，且希望减少文件打开与读取的场景。
---

# SQL72 Parquet split 提交前 Bloom 剪枝

## 说明与范围

输入是经过必要条件、列类型和原生函数验证的绑定描述符。本 skill 负责 split 资格、原类型解析、原求值器调用和提交/跳过决策；不从任意布尔表达式重新提取条件。

## 与相邻技能的分工

本 skill 消费 [Bloom 必要条件与分区绑定](../sql-q72-bloom-partition-binding/SKILL.md) 的结果，负责原类型解析、原探针求值及 split 提交协议，不重复提取表达式。没有有效绑定时保留原提交；绑定生产者不能凭 Bloom 子句存在就代替此处的 split 判断。

这是打开文件前的任务剪枝，与 [运行时过滤下推](../sql-runtime-filter-pushdown/SKILL.md) 的扫描列安装、[连接数量预筛](../sql-q72-join-quantity-pruning/SKILL.md) 的探测行淘汰分别计数。跳过 split 不能直接换算成少做多少次哈希探测或实际读取字节。

## 输入

| 输入 | 核对内容 |
| --- | --- |
| 绑定描述符 | 目标 scan ID、分区列/原类型、原 Bloom/种子/已编译表达式、execution_id 与拥有方 |
| 扫描与任务 | 普通 Hive Parquet、非 Iceberg、目标 TableScanNode、允许的投影链及 split 清单 |
| split 元数据 | 分区键值、路径、起点、长度、文件属性及原 ConnectorSplit 创建/提交协议 |
| 观测材料 | 原提交清单、实际打开/读取字节、输出结果、编译/求值成本及端到端基线 |

## 实施步骤

### 1. 在提交边界确认目标 split

定位 WholeStageResultIterator 组装 ConnectorSplit 之前的入口。确认去掉受支持投影后对应目标扫描 ID，格式是普通 Hive Parquet；Iceberg、自定义 split 或其他扫描走原路径。

### 2. 校验绑定上下文和分区值

消费扫描已有的有效描述符，复核 scan_id、execution_id 及分区列。用目标引擎转换器把分区文本解析到原始列类型，缺键、NULL、类型冲突、解析失败或越界均保留 split。

### 3. 复用原探针并只拒绝确定 false

把原类型值传给绑定的原生表达式，由原转换、带种子哈希和 Bloom 常量求值。只当求值成功且返回非 NULL false 时跳过。true/NULL 及按目标约定可恢复的表达式求值异常按原路径提交，不伪造 0 或用 Python 哈希替代。任务取消、内存不足等致命错误保持原传播协议，不能一概吞掉后继续提交。

### 4. 保持保留任务的元数据与后续过滤

保留 split 的路径、范围、全部分区元数据、文件属性和提交顺序；跳过不应触发读取器创建。原扫描剩余过滤、后续精确连接与任务结束协议继续执行，Bloom true 不能替代精确匹配。

### 5. 验证三值及任务一致性

覆盖肯定拒绝、可能匹配、假阳性、NULL、缺键、32/64 位边界、解析和求值异常、绑定失效、非 Parquet/Iceberg/错扫描。比对保留清单、元数据和最终结果，证明被拒任务未打开。

### 6. 测无剪枝成本与真实 IO

按无/低/高跳过率统计尝试、跳过、实际打开和实读字节，分别测表达式初始化与逐 split 求值。文件总大小只是理论上限；单项开关配对查询才可报告本项收益。

## 约束与回退

- 无绑定、绑定过期或 split 资格不明时沿原提交；不在此 skill 内放宽提取条件。
- 只有成功且非 NULL false 可跳过；未知、异常、NULL 和 true 不构成拒绝证据。
- 保留 split 所有元数据、原剩余过滤、精确连接及资源生命周期，不能仅按名字重建部分 split。

## 交付与验收

交付 `扫描/执行 ID | 尝试/跳过/提交/实际打开 split | 回退原因 | 实读字节 | 初始化/求值耗时 | 结果哈希 | 查询秒数`。分开报告提交计数与真实读取观测。

实现与证据细节见 [实施参考](references/implementation.md)。

## 本地用例与验证边界

从 Skill 根目录运行 `python3 cases/split-pruning-contract/run.py`，按 `cases/split-pruning-contract/case.yaml` 校验结果。使用 `python3 -O cases/split-pruning-contract/run.py` 或 `PYTHONOPTIMIZE=1` 重跑，结果应一致。用例通过普通布尔检查决定 `success` 和非零失败退出码。

用例通过注入三值探针验证提交与回退语义、解析边界和元数据，并检查可恢复异常、致命资源错误传播、全部剪掉及空输入。未打开真实 Parquet，也未验证 Bloom/xxhash 兼容性、原生任务结束信号或实际 IO。未完成的目标端证据标为 `awaiting_target_evidence`。触发与边界评估规格见 [evals/evals.json](evals/evals.json)，规格不等于已执行模型评测。
