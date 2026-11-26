# vsag-rabitq-optimization

VSAG HGraph RaBitQ 并发查询批处理优化总控 Skill。该 Skill 面向 ARM/SVE 平台，覆盖从基线审计到社区提交准备的完整工程流程。

## 适用范围

- VSAG HGraph RaBitQ 并发查询 batching 性能和可移植性。
- `DynamicQueryBatcher` 请求队列、固定生命周期 worker、batch size/timeout 和 deferred state。
- OpenBLAS/CBLAS ROM SGEMM、RaBitQ 4-bit SIMD/SVE lookup、融合 L2、连续 code、scratch、visited 和 heap 优化。
- 编译、正确性、性能、changed-line 覆盖率和开源社区提交验证。

不适用于其他 VSAG 索引或脱离 VSAG batching 场景的通用 BLAS 调优。

## 八阶段流程

```text
1 环境与基线审计
2 后端迁移
3 DynamicQueryBatcher 实现
4 RaBitQ/HGraph 计算优化
5 编译与正确性验证
6 性能测试与调优
7 覆盖率验收
8 社区提交准备
```

阶段 3、4 可以分别迭代；阶段 5 是进入性能测试前的强制门槛；阶段 6 和阶段 7 均通过后才进入阶段 8。

## 资源

- [SKILL.md](SKILL.md)：总控职责、阶段路由和全程硬约束。
- [01-baseline-audit.md](references/01-baseline-audit.md)：环境、commit、数据集、索引、硬件和原始性能。
- [02-backend-porting.md](references/02-backend-porting.md)：KDNN 与 OpenBLAS/CBLAS 路径迁移及私有依赖清理。
- [03-dynamic-query-batcher.md](references/03-dynamic-query-batcher.md)：队列、worker、组批、timeout、deferred state、异常和析构。
- [04-rabitq-hgraph-optimize.md](references/04-rabitq-hgraph-optimize.md)：ROM、SIMD/SVE、L2、code、scratch、visited 和 heap。
- [05-build-correctness.md](references/05-build-correctness.md)：编译、格式/静态检查和正确性矩阵。
- [06-performance-tuning.md](references/06-performance-tuning.md)：16 个连续物理核心、线程和 batch 参数扫描及回归诊断。
- [07-coverage-gate.md](references/07-coverage-gate.md)：干净基线、优化分支和 changed-line ≥90% 门槛。
- [08-community-submission.md](references/08-community-submission.md)：中英文文档、issue、PR、checklist、benchmark YAML、范围声明和回退方案。

## 运行原则

开源路径只使用目标仓库配置的 CBLAS/OpenBLAS，不引入 KDNN 私有代码、符号、路径或环境变量。所有性能结果必须绑定精确 commit、数据集、索引、硬件、NUMA、绑核、search/BLAS/OMP 线程和 batch 配置。未验证的平台不得声称已验证。

