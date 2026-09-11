# 阶段 1：调查与基线

## 执行顺序

1. 用 verbose 确认确实命中 BRGEMM，并记录线程和 blocking。
2. 审计目标 NUMA node、连续核心和其他进程占用。
3. 建立目标 shape × `{plain, prepack}` × 目标线程数的基线。
4. 按 shape family 分组，比较 blocking 与计算/packing 开销。
5. 使用独立 worktree 和构建目录做单变量、交错 A/B。
6. 完成正确性回归并归档 CSV、命令和 verbose。

## Verbose 最小字段

记录 `nthr/nthr_used/nthr_k`、`work`、`m_blk/m_chunk/m_chunks`、`storage_n_blk/compute_n_blk/k_blk/k_chunks`、`pack_a/pack_b/blocked_b`、`copies/reuses`、scratch bytes/reused。

| 症状 | 优先诊断 |
|---|---|
| `nthr_used < nthr` | 空间 work 不足，检查 parallel-K 或 batch×M 合并 |
| `k_chunks > 30 && pack_b=1` | kBlock 太小，PackB 无法摊销 |
| M=32 却使用 M4/M8 tile | 评分函数为填线程过切 M |
| `m_chunks>1` 且几乎无 reuse | PackB 复用 key 或循环顺序错误 |
| storage N64 强制 compute N64 | 存储与计算 block 未解耦 |
| 无 post-op/scale 却 `direct=0` | 存在多余 product buffer 往返 |

## 环境与协议

按 CPU 检查目标 node，而不是只看全局 load。用 `taskset -c <连续空闲核> numactl --membind=<node>`，同时设置 `OMP_PROC_BIND=TRUE` 和 `OMP_PLACES=cores`。每轮前复查这组核心；被占用则整轮作废，不得按数值挑样本。

每个 shape 运行 50 次迭代、3 个独立进程，报告三轮平均时间的中位数和跨轮标准差，禁止用 min。A/B 在同一组核心按 BASE→OPT→BASE→OPT 交错执行。prepack 单列首次 reorder 成本，稳态 execute 不包含它。

## Shape family

- 小 M 大 K：M=32、N=256/512、K≥2048；重点检查 M 不切分、kBlock 与 parallel-K。
- 大 M 中 K：M≥1600、N=128、K≤512；空间 work 足，重点检查大 M block 与 PackB 复用。
- 中型方阵：重点检查二次幂 stride、PackB 和 M chunk。
- 转置 A/K：确认 ISA 路径没有意外禁用 parallel-K。
- 微小矩阵：框架/JIT 固定开销可能吞掉 kernel 收益，默认保留原后端。

使用 `git worktree` 和独立构建目录，不用 stash/checkout 往返。通过独立库目录或 `LD_PRELOAD` 切换，并用 verbose 证明加载的是预期版本。标准差大于差值时结论无效。
