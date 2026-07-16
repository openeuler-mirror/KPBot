# ARM64 Latency Reference For Latency Hiding

This note is a practical scheduling reference for `prefetch-optimization`. It does not replace target-specific benchmarking; use it to decide whether load/FMA interleaving can hide latency before adding or moving prefetches.

## Working Numbers

Use these as conservative starting points when the project has no measured target data:

| Operation | Approximate Latency | Scheduling Note |
|---|---:|---|
| L1 load hit | 3-5 cycles | Usually hidden by 2-4 independent FMA chains. |
| L2 load hit | 10-15 cycles | Needs earlier load issue, prefetch, or more independent accumulators. |
| L3 / LLC hit | 30-50 cycles | Software prefetch or blocking is usually required. |
| DRAM miss | 100+ cycles | Prefetch alone is often insufficient without blocking or data-layout work. |
| FP32 FMA | 4-6 cycles | Keep at least 4 independent accumulator chains when possible. |
| NEON vector load | 3-5 cycles on L1 hit | Load at least one iteration ahead in tight kernels. |

## Load/FMA Interleaving Rules

- Do not emit a long block of loads followed by a long block of FMA unless the target core can queue all loads and the live register set remains below budget.
- Prefer a rhythm such as `load next A/B`, `FMA current accumulators`, `load next A/B`, `FMA next accumulators`.
- For GEMM, convolution, FIR, matrix factorization panels, reductions, and stencil kernels, keep independent accumulator chains so one chain's FMA latency does not serialize the loop.
- Treat prefetch distance and compute distance together: if prefetch is 2 panels ahead, normal loads should still be issued early enough to overlap with current-panel FMA.
- After changing tile size, unroll depth, or accumulator count, compile to assembly and run `register-pressure-analysis`; a schedule that spills can be slower than a less aggressive one.

## Red Flags

- More than 6-8 consecutive vector loads in the hot loop without FMA or address work between them.
- One accumulator reused by every FMA in a reduction or dot-product loop.
- Address-generation instructions placed on the critical path immediately before the dependent load.
- Added prefetches that compete with normal loads in a small-L1 working set.

## Output Guidance

When this reference is used, `prefetch_result` should mention:

- whether `load_fma_interleaving` was applied,
- how many independent accumulator chains are expected,
- whether prefetch distance and compute distance were coordinated,
- whether register-pressure analysis is required before accepting the result.
