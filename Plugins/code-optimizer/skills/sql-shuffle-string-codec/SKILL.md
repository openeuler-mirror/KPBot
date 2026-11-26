---
name: sql-shuffle-string-codec
description: 定位列式 SQL 连接输出与原生 Shuffle 跨阶段的字符串和压缩瓶颈，并选择专用技能组织组合验收。适用于热点尚未定位或多阶段同时变化；已明确的单一物化、写端拷贝、读端重建或 LZ4 候选热点直接使用相应专用 skill。
---

# 字符串与 Shuffle 链路诊断及组合验收

## 职责与拆分关系

保留此已发布名称作为跨阶段诊断入口。原入口中的连接物化、写端固定拷贝、读端重建和 LZ4 候选查找实施步骤，分别由下表四个独立 skill 接管；本入口不再维护它们的第二套实现指南。旧调用仍可从这里开始，但明确单项热点时直接使用对应实施单元。

| 实际问题 | 专用实施单元 | 该单元负责的证据 |
| --- | --- | --- |
| RowContainer 到输出向量的物化或长串引用寿命 | [sql-join-string-materialization](../sql-join-string-materialization/SKILL.md) | 内联布局、独立拥有/retain、输入释放后输出值 |
| 序列化循环中短串变长拷贝 | [sql-shuffle-fixed-width-copy](../sql-shuffle-fixed-width-copy/SKILL.md) | 源目标容量、别名、固定拷贝几何与逐字节一致 |
| 解压后逐条创建字符串视图 | [sql-shuffle-string-reconstruction](../sql-shuffle-string-reconstruction/SKILL.md) | 实际可读边界、布局/ISA、输出引用寿命 |
| 压缩器候选哈希查找 | [lz4-hash-candidate-optimization](../lz4-hash-candidate-optimization/SKILL.md) | 表模式、加载边界、真实匹配及四向编解码 |

## 输入

读取实际 SQL/最终计划、Exchange 和文件 codec、实际加载库及源码提交、阶段热点、长度分布、缓冲区拥有方、局部与完整查询基线。没有计划或热点证据时先定位，不能把输入 Snappy Parquet 推断成中间 Shuffle 不使用 LZ4。

## 诊断与选择

1. 沿 `文件读取 → 算子输出 → Shuffle 写入 → 压缩/传输 → 解压 → 重建列` 标出真实入口与计时边界，分开文件 codec 和 Exchange codec。
2. 将热点归入上表，读取对应专用 skill。单项任务只进入相关单元；不因链路使用 LZ4 就同时修改物化、写入和读端。
3. 多阶段同时变化时列出相互传递的缓冲、字节布局及拥有方。每阶段分别给出准入、实际命中与回退；任何未证明安全的阶段保留原实现，其余单项可独立验证。
4. 先完成各单元自己的结果和边界验收，再组合回放同一批真实输入。不要用写端容量证明读端可读范围，也不要用压缩器往返证明 StringView 的生命周期。

## 组合验收与输出

组合对照保留行值、NULL、顺序、重数及各缓冲销毁后的可用性。序列化协议要求逐字节一致时比较字节；压缩流是否允许改变及真实四向矩阵由 LZ4 单元验证，组合后还要经实际 reader 比较完整行值。

输出 `实际调用链 | 选择的实施单元及理由 | 各阶段准入/回退/命中 | 缓冲交接契约 | 局部结果与成本 | 单项开关对照 | 完整查询配对结果`。单项归因分别切换候选，合并成绩按合并候选报告，不能把总耗时变化重复分配给多个单元。

## 保留的兼容基线与历史资料

既有 `cases/shuffle-contract` 继续保留为旧入口的合成回归基线；它验证拷贝几何和简化读端分派，不是新的路由实现或四单元组合验证。65/17/88/44/22 的拷贝矩阵已迁入写端单元并增加别名回退检查，相同矩阵不重复计作独立安全证据。本入口的兼容用例覆盖 35 组共享缓冲区拷贝，并连续执行六次变长字段的左右交错搬移。每次按操作前的预期字节比对整个缓冲区。目标视图容量少一个字节时，还要核对写入被拒绝且数据完整。

从本目录运行 `python3 cases/shuffle-contract/run.py`，按 `cases/shuffle-contract/case.yaml` 核对结果；`python3 -O cases/shuffle-contract/run.py` 与 `PYTHONOPTIMIZE=1` 仍应保留失败检测。各实施单元另有独立 cases。路由评估规格见 [evals/evals.json](evals/evals.json)，规格不是已执行的模型评测。

对照既有验收记录时读取 [历史内存与兼容性检查](references/implementation-validation.md)，定位源码及历史结果时读取 [Q67 案例](references/velox-q67.md)。它们保留用于追溯；当前实施以专用单元及目标锁定版本为准。合成通过不代表原生内存、ISA、真实 LZ4 或性能验证通过，缺少的目标端证据标为 `awaiting_target_evidence`。
