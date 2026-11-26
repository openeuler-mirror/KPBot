---
name: sql-q72-bloom-partition-binding
description: 从 Gluten/Velox 扫描剩余过滤提取可独立否决 split 的原生 Bloom 必要条件，并绑定整型分区列。适用于准备把日期 Bloom 用于提交前剪枝，需要证明 AND、函数身份、种子、转换和扫描归属的场景。
---

# SQL72 Bloom 必要条件提取与分区列绑定

## 说明与范围

本 skill 输出已验证的 Bloom 绑定描述符，不读取 split，也不决定跳过文件。描述符可交给 split 提交前剪枝实现；缺少资格证据时输出不适用及原因。

## 与相邻技能的分工

本 skill 是 [split 提交前剪枝](../sql-q72-parquet-split-pruning/SKILL.md) 的绑定生产者，只交付经过验证的原表达式、分区列和扫描/执行上下文；不读取分区值或跳过 split。消费者解析元数据并决定提交或拒绝，不能反过来放宽本 skill 的必要条件准入。

[运行时过滤下推](../sql-runtime-filter-pushdown/SKILL.md) 负责追踪过滤如何到达扫描原列；这里从已进入扫描剩余过滤的原生 Bloom 开始。缺少扫描过滤时先追踪下推，不能根据 SQL 模板虚构 Bloom 或绑定。

## 输入

| 输入 | 核对内容 |
| --- | --- |
| 计划与表达式 | 目标 scan ID、NativeFilters、ReadRel、HiveTableHandle remainingFilter 及编译后表达式 |
| 列与 schema | HiveColumnHandle 分区标记、唯一字段绑定、原始 INTEGER/BIGINT 类型及别名映射 |
| 函数与常量 | 原生 might_contain、xxhash64_with_seed 的身份、确定性、Bloom 常量与常量种子 |
| 上下文 | 目标提交、编译/执行上下文、常量缓冲拥有方和计划重建/任务结束时机 |

## 实施步骤

### 1. 定位扫描自身的剩余过滤

从 BatchScanTransformer 的 NativeFilters 追到 ReadRel、SubstraitToVeloxPlan 和 Hive 句柄。以 scan ID 和列句柄匹配当前扫描；同名字段不足以证明是同一分区列。

### 2. 仅沿必要条件提取候选

允许根表达式或顶层连续 AND 子树中的原生 Bloom 子句。不能穿过 OR、NOT、UDF 或不支持的结构；false OR other 不能独立否决。确认目标引擎允许该必要条件前移且保留既有错误/求值契约。

### 3. 核对函数、常量和输入字段

确认原生函数身份、Bloom 位图常量、常量 int64 种子和唯一分区字段。拒绝动态种子、非确定性函数、数据列、其他扫描列和模糊别名。使用原始 Bloom 常量和哈希，不自行实现兼容性未经证明的探针。

### 4. 保留原始列类型及合法扩宽

只准入直接 INTEGER/BIGINT 或探针内部已证明保值的 INTEGER→BIGINT。描述符同时保存 source_bits 和 hash_bits；解析分区值仍用原列类型，不能因 hash 用 BIGINT 就允许原 INTEGER 越界值。

### 5. 编译后复核并绑定生命周期

复核已编译表达式的函数身份、字段及确定性，保存原表达式/常量拥有方。描述符携带 scan_id、字段、原类型、哈希类型、种子、Bloom 和 execution_id。新计划、扫描替换或上下文销毁后失效并重新绑定。

### 6. 验证输出与拒绝原因

覆盖正向绑定、嵌套 AND、OR/NOT、别名错绑、普通数据列、其他扫描、缩窄转换、动态种子和编译后不匹配。交付描述符与拒绝理由，原剩余过滤原样保留。

## 约束与回退

- 提取只产生可消费的必要条件；本阶段不能删除 split 或替代原扫描剩余过滤。
- 绑定以实际 scan ID、列句柄和原生函数身份为证据，不能只匹配函数名/字段名字符串。
- 无候选或编译/确定性/类型证据不足时返回无绑定，消费者提交原 split。

## 交付与验收

交付 `scan_id | 分区列/原类型 | 必要条件位置 | 函数/种子/Bloom | 编译复核 | execution_id/拥有方 | 绑定或拒绝原因`。用原表达式与描述符一一对应，不能仅输出 eligible=true。

实现与证据细节见 [实施参考](references/implementation.md)。

## 本地用例与验证边界

从 Skill 根目录运行 `python3 cases/bloom-binding-contract/run.py`，按 `cases/bloom-binding-contract/case.yaml` 校验结果。使用 `python3 -O cases/bloom-binding-contract/run.py` 或 `PYTHONOPTIMIZE=1` 重跑，结果应一致。用例通过普通布尔检查决定 `success` 和非零失败退出码。

用例验证表达式结构、类型和绑定描述符模型，未执行真实 Substrait 解析、原生编译器、xxhash 或 Bloom。 未完成的目标端证据标为 `awaiting_target_evidence`。触发与边界评估规格见 [evals/evals.json](evals/evals.json)，规格不等于已执行模型评测。
