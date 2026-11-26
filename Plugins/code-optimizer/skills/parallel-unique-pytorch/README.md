# parallel-unique-pytorch

PyTorch embedding lookup 流水线的并行 unique 优化 Skill：用 `__gnu_parallel::sort` + OpenMP 替代单线程的 `np.unique`/`torch.unique`，并以 `TORCH_LIBRARY` + `register_fake` 整合为支持 `torch.compile` 的 `torch.ops.*` 自定义算子。

## 适用范围

- host 侧 raw_id → unique → hashmap → gather 流水线中 `np.unique`/`torch.unique`（CPU）热瓶颈。
- unique 必须留在 host 的场景（下游 hashmap 无 device 版本）。
- 已有 pybind11/numpy C++ 扩展，需要改造成支持 torch.compile 的自定义算子。

不适用于：unique 可搬上 CUDA、n < 2048（OpenMP 启动开销主导）、int32/int64 之外的 dtype。

## 资源

- [SKILL.md](SKILL.md)：触发条件与拒绝边界、6 条核心实测结论（单线程基线、inverse 带宽瓶颈、85% 收益来自 cache locality、拐点区间、concat-then-unique、线程封顶）、5 步方法论、PyTorch 整合 checklist（含两个注册陷阱与 register_fake 惯用法）、9 条反模式、benchmark 协议。

## 验证结论（2026-09，192 核 aarch64，GCC 12 + PyTorch 2.6 CPU）

- 按 SKILL.md checklist 从零搭建实现验证，16/18 项 claim 通过；两条未通过项均已作为机器相关性 caveat 写回 SKILL.md。
- 正确性 24/24（vs `torch.unique`/`np.unique`，int32/int64 × 1D/2D/空 × 3 分布）；错误输入全部 RuntimeError；`torch.compile(dynamic=True)` 一次通过。
- 拐点复现：n=1024 亏（0.6x）→ 4096 临界（1.5-1.7x）→ ≥65536 赢（5.0-5.5x）→ n=1M op 加速 4.4-6.3x；inverse 占 op 50-53%，为最大单一阶段。
- unique 入场费从 ~76ms 降至 ~3.4ms（n=1M），验证"入场费降到噪声级"心智模型。
