# 迁移交接单

## 1. 身份与范围

- migration_id：
- engine / adapter：
- source：`<version>@<commit>`（dirty：<true|false>）
- target：`<version>@<commit>`（dirty：<true|false>）
- target baseline：
- 算法 / opt_set / load-save / metric / 数据集：

## 2. 结论摘要

- 一句话结论：
- 机制计数：KEEP `<n>` / ADAPT `<n>` / SPLIT `<n>` / OPT_IN `<n>` / REPLACE `<n>` / DROP `<n>` / DEFER `<n>`
- 门禁结果：G0 `<pass|fail|blocked>` / G1 `<...>` / G2 `<...>` / G3 `<...>` / G4 `<...>` / G5 `<...>`
- 收益 geomean 与每数据集最低值（带证据标记）：

## 3. 门禁证据

| 门禁 | 结论 | 原始产物路径 | 备注 |
| --- | --- | --- | --- |
| G0 feature-off |  |  |  |
| G1 UT/ST |  |  |  |
| G2 内核位级探针 |  |  |  |
| G3 端到端 top-k dump |  |  |  |
| G4 recall 零下降 |  |  |  |
| G5 同窗 ABBA |  |  |  |

## 4. Patch 序列

| 序号 | 名称 | 依赖 | 宏/开关 | 作用点 | 回退方式 |
| --- | --- | --- | --- | --- | --- |
| 01 |  |  |  |  |  |

## 5. Open items

| 编号 | 描述 | 当前处置 | 触发条件 | 复测门槛 |
| --- | --- | --- | --- | --- |
| O1 |  | DEFER/OPT_IN |  |  |

## 6. 知识回写

- [ ] 目标版本新事实已回写到对应算法 Skill 的 experiments reference。
- [ ] 每条结论带 `[source]` / `[experiment]` / `[inference]` / `[unverified]`。
- [ ] 未把 provisional / uncontested 结论写成 verified。
- [ ] 未迁移个人路径、历史实验台账与不可复现的绝对 QPS。

## 7. 产物索引

- 身份卡：`SOURCE-TARGET-IDENTITY.json`
- 追溯矩阵：`MIGRATION-MATRIX.md`
- patch：`patches/`
- 门禁日志：`G0-feature-off/` … `G5-perf/`
- 版本审计：`version-delta.json`
- 构建一致性：`compile-flags.json`
