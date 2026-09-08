---
name: vsag-rabitq-optimization
description: >
  工作场景：编排 VSAG HGraph RaBitQ 在 ARM 平台上的并发查询批处理迁移与优化，覆盖 OpenBLAS 后端、DynamicQueryBatcher 和 RaBitQ/HGraph 计算路径。
  适用阶段：按需执行环境与基线审计、后端迁移、Batcher 实现、计算优化、编译与正确性、性能、覆盖率和社区提交阶段。
  不适用的反例：其他 VSAG 索引类型、脱离 VSAG batching 场景的通用 BLAS 调优，以及非 ARM/SVE 性能优化。
---

# VSAG RaBitQ 优化总控

本 Skill 是唯一注册入口。根据用户任务读取对应阶段 reference；不要在启动时加载全部阶段。完整流程按顺序执行，局部任务只读取其阶段及必要上游阶段。

## 总体流程

```text
1 环境与基线审计
        ↓
2 后端迁移
        ↓
3 DynamicQueryBatcher 实现 ←→ 可独立迭代
        ↓
4 RaBitQ/HGraph 计算优化  ←→ 可独立迭代
        ↓
5 编译与正确性验证        ← 性能测试前硬门槛
        ↓
6 性能测试与调优
        ↓
7 覆盖率验收
        ↓
8 社区提交准备             ← 第 6、7 阶段通过后才可进入
```

## 阶段路由

| 阶段 | 何时读取 | Reference | 主要产出 |
|---|---|---|---|
| 1 | 任何新任务、基线不完整或配置有疑问 | [01-baseline-audit.md](references/01-baseline-audit.md) | 可复现的环境与基线记录 |
| 2 | KDNN/私有后端迁移或开源可移植性检查 | [02-backend-porting.md](references/02-backend-porting.md) | OpenBLAS/CBLAS 迁移与残留检查 |
| 3 | 实现或修复并发组批机制 | [03-dynamic-query-batcher.md](references/03-dynamic-query-batcher.md) | 正确的队列、worker 和 deferred state |
| 4 | 优化 ROM、RaBitQ lookup 或 HGraph traversal | [04-rabitq-hgraph-optimize.md](references/04-rabitq-hgraph-optimize.md) | 经证据选择的计算优化 |
| 5 | 编译、格式检查或正确性验证 | [05-build-correctness.md](references/05-build-correctness.md) | 构建与正确性门槛结论 |
| 6 | benchmark、参数扫描或跨机器退化诊断 | [06-performance-tuning.md](references/06-performance-tuning.md) | 可复现性能矩阵与归因 |
| 7 | 覆盖率采集或 changed-line 门槛检查 | [07-coverage-gate.md](references/07-coverage-gate.md) | 基线/分支覆盖率及 ≥90% 结论 |
| 8 | 准备 issue、PR、文档和 benchmark 材料 | [08-community-submission.md](references/08-community-submission.md) | 完整社区提交包 |

## 硬门槛

- 阶段 1 未固定 commit、数据集、索引和运行配置时，不得声称性能改善。
- 阶段 2 的社区路径不得残留 KDNN 或其他私有后端代码、符号、路径、环境变量和链接依赖。
- 阶段 3、4 可以分别迭代，但任何代码变更都必须回到阶段 5。
- 阶段 5 编译、相关测试或正确性未通过时，不得进入阶段 6。
- 阶段 6 性能结论和阶段 7 changed-line 覆盖率均通过后，才可进入阶段 8。
- commit、push、DCO sign-off 或对外提交仅在用户明确授权后执行。

## 全程不变量

- 不改变序列化索引兼容性，不静默改变 benchmark 或 recall 口径。
- 不混用不同 commit、batch flag、数据集、索引、绑核、NUMA、search threads、BLAS threads 或环境变量下的结果。
- 保留无关工作树变更，一次只验证一个边界清晰的优化。
- ARM/SVE 专属结果必须声明平台范围；未运行 x86、无 SVE ARM 或 TSAN 时不得标记为已验证。

