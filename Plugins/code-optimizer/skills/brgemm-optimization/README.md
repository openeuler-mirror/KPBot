# BRGEMM Optimization Skill

面向鲲鹏/ARM 平台上 BRGEMM MatMul/Gemm 框架集成的专项优化 Skill。它把原始实践整理为一个总控入口和八个按需读取的阶段，覆盖执行路径调查、Pack/布局、内存、线程、框架缓存、正确性、性能验收以及证据交付。

## 结构

```text
brgemm-optimization/
├── README.md
├── SKILL.md
└── references/
    ├── 01-investigation-and-baseline.md
    ├── 02-pack-and-layout.md
    ├── 03-buffer-and-copy.md
    ├── 04-threading-and-blocking.md
    ├── 05-cache-and-framework.md
    ├── 06-build-and-correctness.md
    ├── 07-performance-acceptance.md
    └── 08-evidence-and-handoff.md
```

`SKILL.md` 只负责识别任务、维护阶段状态和按需路由；执行具体工作时只加载当前阶段及其直接依赖。阶段 6 是进入性能验收前的强制门槛，阶段 7 的全 shape 防回退通过后才能完成交付。

## 适用边界

- 适用：BRGEMM kernel 集成至 oneDNN adapter、ONNX Runtime EP 或自研 runtime 后的 CPU 性能与可移植性优化。
- 重点：PackB/PrePack、blocked layout、scratch、parallel-K、blocking、JIT/primitive cache、框架路由与严格 A/B。
- 不适用：BLAS 内部 microkernel 微架构开发、GPU kernel、与 MatMul/Gemm 无关的通用优化。

## 来源

内容总结自 `kdnn_brgemm_integrate` 的 `brgemm-ort-integration` 分支及配套 ORT P0–P3 实测记录。阶段 8 保留 E1–E15 的提交、数据和适用性边界，避免把结构性改动或未独立测量的结果误报为性能收益。
