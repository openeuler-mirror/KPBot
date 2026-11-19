# 向量数据库 / 向量检索域

ANN 索引、向量库与检索服务的性能优化。与业务侧（搜推 DAG、embedding 服务）解耦：本域只对"向量检索这一层"负责，
换一个向量库产品仍然成立的问题属于本域。

## 成员

| Skill | 职责 |
|---|---|
| [`vsag-rabitq-optimization`](vsag-rabitq-optimization/SKILL.md) | VSAG HGraph RaBitQ 在 ARM 平台上的并发查询批处理迁移与优化：OpenBLAS 后端、DynamicQueryBatcher、RaBitQ/HGraph 计算路径，按环境与基线审计 → 后端迁移 → Batcher 实现 → 计算优化 → 编译与正确性/性能/覆盖率 → 社区提交分阶段推进 |

本域后续迁入（逐个独立 PR，均只动各自的 `skills/vectordb/**`）：

- 通用方法与编排：`vector-search-optimize`（CP0–CP7）、`vector-experiment-methodology`、`vector-ut-st-validation`、`vector-db-optimization-migration`
- 引擎专项子域：`faiss/`（13 个）、`diskann/`（3 个）

## 成员登记纪律

- 新增成员只在本文件（或引擎子域的 `DOMAIN.md`）登记，不要改 `skills/kpbot-code-optimizer/SKILL.md` 的全局路由表——
  共享行会让并行 PR 必然冲突。
- 目录约定：`skills/vectordb/<skill>/SKILL.md`，引擎专项再深一层 `skills/vectordb/<engine>/<skill>/SKILL.md`；
  域目录必须带 `DOMAIN.md`（安装器据此识别为分组）。

## Review

- owners: []（待指派，填写 GitCode 用户名）
- 评审范围：`Plugins/code-optimizer/skills/vectordb/**`
