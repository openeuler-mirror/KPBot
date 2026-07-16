# Register Accumulation Guide

This guide extends `apply-vectorization` from simple map/reduction loops to compute-intensive kernels where performance depends on keeping accumulators live in vector registers across an inner loop.

## What To Detect

Register accumulation is required when the hot loop repeatedly updates the same output tile or scalar result:

- GEMM / batched GEMM: `C[m,n] += A[m,k] * B[k,n]`
- convolution / depthwise convolution: `out[c] += input[c + offset] * weight[offset]`
- FIR / filter taps: `acc += sample[i + tap] * coeff[tap]`
- matrix factorization panels: repeated rank-k updates
- reductions and dot-products: `sum += a[i] * b[i]`
- Stencil kernels: fixed neighbor terms accumulated into one output point

## Required Rule

If the optimized form has a hot K/tap/radius loop, accumulators must stay in registers until the accumulation domain is complete.

Reject or mark as unverified when generated code:

- stores a partial accumulator to memory inside the K/tap/radius loop,
- reloads the same partial accumulator before the next FMA,
- uses only one accumulator chain when multiple independent chains are needed to hide FMA latency,
- increases accumulator count without a register-budget check,
- keeps a safe but underutilized accumulator count when a larger non-spill-likely register allocation is available.

For compute kernels, "no spill" is not enough. A 4xVL SVE kernel with four accumulators can be safe but still leave FMA throughput underused. Run `scripts/select_register_allocation.py` before code generation and prefer the highest eligible throughput score under the selected spill-risk and register-class budgets.

## Acceptable Patterns

### NEON FP32 Dot Or Row Kernel

```c
float32x4_t acc0 = vdupq_n_f32(0.0f);
float32x4_t acc1 = vdupq_n_f32(0.0f);
for (int k = 0; k < K; k++) {
    float32x4_t b0 = vld1q_f32(b + k * ldb + n);
    float32x4_t b1 = vld1q_f32(b + k * ldb + n + 4);
    float32x4_t a0 = vdupq_n_f32(a[m * lda + k]);
    acc0 = vfmaq_f32(acc0, a0, b0);
    acc1 = vfmaq_f32(acc1, a0, b1);
}
vst1q_f32(c + m * ldc + n, acc0);
vst1q_f32(c + m * ldc + n + 4, acc1);
```

The important property is not the exact tile shape. It is that `acc0` and `acc1` are live across all `K` iterations and written once at the end.

### Stencil Or Filter

For fixed small radius or tap count, each output lane should have one or more register accumulators. The source samples and coefficients may be loaded per tap, but the output accumulator remains live until all taps have contributed.

## JSON Output Guidance

When this rule matters, fill optional `vectorization_result.accumulation_pattern`:

```json
{
  "accumulation_pattern": {
    "kind": "register_accumulation",
    "domain": "k_loop|filter_taps|stencil_radius|reduction",
    "accumulators": 8,
    "kept_live_until": "tile writeback",
    "memory_roundtrip_in_inner_loop": false
  }
}
```

Also add a `safety_checks` entry explaining the accumulation domain and writeback point.
When a higher-scoring eligible shape exists, mark lower-accumulator candidates in `candidate_register_allocations` with `underutilization_risk=true`. Keep `fallback_register_allocations` so verification can reduce shape when spills or clobber issues appear.

## Verification

After materialization:

1. Compile to assembly with the same target flags used by benchmark.
2. Run `register-pressure-analysis` when available.
3. Inspect the hot loop for `str/stp` to `sp` or accumulator stores inside the K/tap/radius loop.
4. If spills appear, reduce micro-kernel shape, accumulator count, or unroll depth.
5. If no spills appear but performance is far below a library kernel, compare accumulator count and pressure ratio; a low-risk 4 accumulator shape may need to be replaced by a medium/high-risk selected allocation.
