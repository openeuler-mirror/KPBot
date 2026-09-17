# 内存分配器选型决策指南

> 综合各场景的 allocator 选型经验，给出场景到分配器的决策路径。本决策树属经验推荐，`confidence` 最高为 `experience_only`；现场判定需本轮 perf 证据。

## 决策树

```
场景判断:
├── AI 推理 (vLLM/SGLang)
│   ├── host 侧 malloc 优化 → tcmalloc (BiSheng 优先，见 ascend-playbook.md "tcmalloc 运行时依赖陷阱")
│   └── device 侧显存 → NPUAllocator (torch_npu 内置，配合 PYTORCH_NPU_ALLOC_CONF)
│
├── AI 训练 (CANN runtime 锁竞争 / hostbound)
│   ├── hostbound 训练（Computing/Stage 偏低、Free/Stage 偏高、
│   │   大量算子 device_total=0）→ tcmalloc 优先
│   │   （见 ascend-playbook.md "A+K 训练场景 Host 侧 Malloc 优化"）
│   └── tcmalloc_for_cann 或 jemalloc (需现场 perf 对比)
│
├── 数据库 (MySQL/PG/Redis)
│   └── jemalloc (大页支持好，碎片控制优)
│
├── 通用高并发 (Web/微服务)
│   └── tcmalloc (线程缓存，多线程高并发锁竞争低)
│
└── 内存带宽密集 (向量计算/矩阵运算)
    └── jemalloc 2MB 大页 (减少 TLB miss)
```

## 决策依据映射

| 决策路径 | 关键证据 | 验证方法 |
|---------|---------|---------|
| AI 推理 → tcmalloc | perf 热点 `__pthread_mutex_lock` + lsof 命中 CANN DSO | LD_PRELOAD + 吞吐对比 ≥3 次 |
| AI 训练 → tcmalloc_for_cann | perf 热点 CANN DSO 内容器操作锁 | 同上，需确认 CANN 版本兼容 |
| AI 训练 hostbound → tcmalloc 优先 | hostbound 信号（Computing/Stage 低、Free/Stage 高、大量算子 device_total=0）+ perf 热点 `malloc`/`__lll_lock_wait_private` 跨多线程出现 | LD_PRELOAD + 稳态 TPS 对比 ≥3 次（step 20+ 稳态阶段采样） |
| 数据库 → jemalloc | perf 热点 malloc/free + 大对象分配占比高 | LD_PRELOAD + QPS 对比 |
| 通用高并发 → tcmalloc | 线程数 >32 + malloc 热点 >5% | LD_PRELOAD + RPS 对比 |
| 内存带宽密集 → jemalloc 大页 | perf mem 事件 TLB miss 高 | LD_PRELOAD + 带宽 benchmark |

## 跨场景经验参考（仅作优先级依据，非判定依据）

- Ascend910 + vLLM 推理：tcmalloc > jemalloc（同 PGO 环境下 tcmalloc 优 4.26%）
- A+K（Ascend NPU + 鲲鹏 CPU）hostbound 训练：tcmalloc 稳态 TPS +30% 量级（openEuler/aarch64）；训练收益通常高于推理，不可跨场景外推
- 数据库场景：jemalloc > tcmalloc（大页+碎片控制优势）
- 通用高并发 Web 服务：tcmalloc 通常优于 glibc，与 jemalloc 需现场对比

> 历史跨场景经验可作为推荐优先级依据，但**不可直接用作现场判定结论**。现场判定必须基于本轮 `current_run_id` 的 perf 采集 + 实测对比。
