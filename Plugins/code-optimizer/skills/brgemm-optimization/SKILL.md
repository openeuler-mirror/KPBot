---
name: brgemm-optimization
description: >
  工作场景：鲲鹏/ARM 上 BRGEMM MatMul/Gemm 的性能调查、PackB/PrePack 与 blocked layout、
  scratch、parallel-K、blocking、JIT 缓存和推理框架集成优化。适用阶段：阶段 1-8
  全流程或其中任一阶段，适用于 oneDNN adapter、ONNX Runtime EP 和自研 runtime。
  不适用的反例：BLAS 库内部 microkernel 微架构开发、GPU kernel、非 MatMul/Gemm
  工作负载或脱离真实 shape 的通用参数调优。
---

# BRGEMM 优化总控

本 Skill 是一个按任务路由的总控入口。不要预先加载全部 reference；先判断当前状态，只读取当前阶段、它的直接依赖和用户明确要求的材料。

## 阶段路由

| 阶段 | 何时读取 | Reference | 退出条件 |
|---|---|---|---|
| 1 调查与基线 | 开始任何优化、结果不稳定或执行路径不明 | [01](references/01-investigation-and-baseline.md) | 环境、shape、plain/prepack 基线和 verbose 已归档 |
| 2 Pack 与布局 | PackB、PrePack、blocked layout 或 stride 可疑 | [02](references/02-pack-and-layout.md) | copy/reuse、layout key 和 blocking 耦合已验证 |
| 3 Buffer 与 copy | scratch、内存占用、copy、direct output 或 batch 合并 | [03](references/03-buffer-and-copy.md) | buffer 所有权、覆盖写和并发 lease 有测试 |
| 4 线程与 blocking | 线程利用率低、M 被过切或需要 parallel-K | [04](references/04-threading-and-blocking.md) | family 计划、线程数和同步路径已固化 |
| 5 缓存与框架 | JIT/primitive cache、PrePack 生命周期、路由或融合 | [05](references/05-cache-and-framework.md) | key、锁粒度、一次性成本和 fallback 明确 |
| 6 编译与正确性 | 代码完成后、性能测试前 | [06](references/06-build-and-correctness.md) | 全部构建、数值、blocking 与并发测试通过 |
| 7 性能验收 | 正确性门槛通过后 | [07](references/07-performance-acceptance.md) | 固定核 A/B、全 shape 防回退达到预注册门槛 |
| 8 证据与交付 | 结论归档、评审或移交 | [08](references/08-evidence-and-handoff.md) | 每项结论可追溯，已标明证据等级和局限 |

依赖顺序：`1 → 2/3/4 → 5 → 6 → 7 → 8`。阶段 2、3、4 可以按瓶颈反复迭代；阶段 6 是进入阶段 7 的强制门槛。

## 始终遵守

1. 阻塞启发式必须用目标 workload 的真实 shape 验证；不得照抄上游参数后直接归因架构。
2. 性能结论至少使用 50 次迭代 × 3 个独立进程，以平均时间的中位数报告；固定空闲连续核并交错执行 A/B。
3. 先用 verbose 确认 `nthr_used/m_blk/n_blk/k_blk/nthr_k/pack_b/copies/reuses`，再提出瓶颈假设。
4. 改变布局时同时比较对照、plain 和 prepack；一次只改一个变量。
5. mutex 只保护资源租用或缓存元数据，不得覆盖整个计算阶段。
6. 未通过阶段 6 不得宣称性能结论；没有原始数据、环境和提交号不得写入交付结论。
