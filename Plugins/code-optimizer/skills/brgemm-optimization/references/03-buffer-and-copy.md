# 阶段 3：Buffer 与 copy

## Scratch pool

每个 MatMul 对象维护 scratch pool。`Acquire(elements)` 只在挑选容量足够的空闲 buffer 时持锁，返回的 shared lease 在整次执行期间独占；同一对象被外部线程并发调用时必须获得不同 buffer。使用对齐分配，不做无依据的全量清零。

省略清零前逐项证明首次写覆盖：packed A/B 由 copy kernel 覆盖；product 首个 K chunk 用 `accumulate=false` 覆盖；reduced product 由 memcpy 初始化；dst 仅在语义需要旧值时读取。测试连续两次 `reused=0→1` 和并发 lease 隔离。

## PackB 单槽与 PackA 时序

当执行循环按 `K→N→M` 覆盖 B tile 时，默认每 worker 一个 PackB slot。只有 `N_chunks==1`、worker 跨多个 work 且 `maxKChunksPerThread==1` 时才保留整个 N chunk 的多槽缓存；最后一个 N block 不能误判为完整 chunk。

PackA 在当前 N chunk 的首个 N block 前完成，并立即计算；后续 N block 复用同一 packed-A。它只改变时序，不应改变 pack 次数、所有权或结果。

## Direct output 与非连续 dst

当 dst 列连续、`kThreads==1`、unit scale、`beta==0` 且 post-op 可直接寻址时，BRGEMM 直接写 dst。非连续 dst 走 gather→postwork→scatter，但只有 `beta!=0` 或 SUM 才读取旧值；`beta=0` 时执行 `0*NaN` 会污染结果。

buffered/parallel-K 的最终 epilogue 可用 BRGEMM JIT 的 `BS=0 + skipAccumulation`，避免维护第二套 post-op 实现。

## Copy 与 batch×M

不要把 copy 指令微调当主要优化：原始 plain copy 与 SVE256 8×8 transpose fast path 的 copy-only 差异只有约 ±0.3%。先减少 copy 调用次数和工作集。

仅当 B 在所有 batch 维广播、A/C dense plain 且 batch/行连续、bias 为跨 batch 的 1×N、post-op 只含 SUM/eltwise 或 scalar/no-broadcast binary 时，才合并为有效 `M=batch×M`。PReLU 或 per-N broadcast 依赖原 batch 寻址，必须阻止合并。测试广播 B 等价平铺结果和反例 `merge_batch_m=0`。
