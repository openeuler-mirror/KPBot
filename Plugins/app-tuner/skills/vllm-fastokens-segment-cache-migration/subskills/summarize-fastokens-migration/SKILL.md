---
name: summarize-fastokens-migration
description: >
  根据已有探测、候选验证、回放和快照生成中文迁移报告，说明是否具备切换条件。
---

# 迁移评估报告

## 操作流程

1. 清点已有产物，核对环境、候选版本、模型或分词器和回放场景是否对应。来源不同的结果分别展示；只做报告时不重跑探测或回放。

   | 报告内容 | 证据 |
   |---|---|
   | 迁移适用性、当前后端、候选加载 | 迁移前和候选加载的 `probe.json` |
   | 一致性、并发稳定性、缓存命中 | `report.json` 的 `token_consistency`、`concurrency_acceptance`、`problems`、各组 `concurrency` 与开启组 `stats` |
   | 分词耗时和 RSS | `report.json` 的基线组、开启组及本次回放参数 |
   | 回退准备与执行状态 | 快照的 `fastokens.json`、`env.json`、`RESTORE.md`；切换后、回退后的 `probe.json` |

2. 分开给出回放验证、性能验收和回退准备结论。[回放验收](../verify-tokenizer-replay/SKILL.md#回放验收)未通过或缺少证据时，不建议切换。旧报告若只有 `correctness_pass`，仍要核对两组实际的 `concurrency`。未给定阈值、RSS 为 `null`，或旧报告的 `0 MiB` 无法证实为实测时，相关性能项记为待确认，不判定达标。
3. 按父技能的[报告结构](../../SKILL.md#报告要求)生成 `summary.md`。数值注明文件、基线、分词器层和场景，标明未执行或无法确认的事项。只有满足验收条件且回退准备齐全时才建议切换，将安装版本、段缓存预算、配置位置和 `RESTORE.md` 写入方案。服务切换和回退由用户执行。
