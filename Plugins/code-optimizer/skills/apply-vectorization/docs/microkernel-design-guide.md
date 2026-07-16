# Micro-kernel Design Guide

This guide defines conservative tile and micro-kernel rules for compute-intensive kernels handled by `apply-vectorization`.

## Scope

Use these rules for:

- GEMM, GEMV, rank-k, and matrix-factorization panel updates
- convolution and depthwise convolution
- FIR/filter kernels
- reductions and dot-products
- fixed-radius stencil kernels

The goal is to choose a tile shape that exposes enough independent FMA work without falling into a spill-likely register budget. A shape that is safe but leaves too few accumulators live is still a performance risk.

## Tile Shape Selection

Choose tile shape in this order:

1. Identify the accumulation domain: K, taps, radius, or reduction length.
2. Identify output tile dimensions: rows x columns, channels x pixels, or points x vectors.
3. Compute accumulator registers:
   - vectorized N dimension: `accumulators = M * ceil(N / lanes_per_vector)`
   - multiple output channels or stencil points count as additional `M`
4. Add temporary registers for loads, broadcast, masks, and epilogue.
5. Run `scripts/select_register_allocation.py` to enumerate candidate MxN shapes and select the highest verifiable throughput score under all register-class budgets.
6. Generate the micro-kernel using `selected_register_allocation.shape`; `microkernel_hint.shape` is only a candidate, not the final answer.
7. If the selected risk is `medium` or `high`, request assembly spill analysis during verification. If verification finds spills or accumulator memory roundtrips, retry with `fallback_register_allocations` in order.

Example:

```bash
python3 scripts/select_register_allocation.py --isa sve --dtype float32 --n 8 --json
python3 scripts/select_register_allocation.py --isa sve --dtype float32 --shape-candidates 8x8,12x8,16x8 --json
```

## Starting Points

These are starting points, not guarantees:

| ISA | dtype | Suggested Starting Shape | Notes |
|---|---|---|---|
| NEON | float32 | select from 4x4 through 24x4 | 4 lanes per vector; low shapes such as 4x4 are underutilized when larger candidates fit. |
| NEON | float16/bfloat16 | 4x8 or 8x8 | More lanes, but widening accumulators may double pressure. |
| SVE | float32 | select from 4xVL through 24xVL | Prefer scalable N dimension and predicate tails; 16xVL or 20xVL may be valid when spill risk allows. |
| SME ZA | float32 | tile by ZA shape | Only for clear outer-product semantics and verified ZA ownership. |

## Micro-kernel Rules

- Accumulators stay live in vector registers across the full K/tap/radius domain.
- Do not stop at the first safe accumulator count. Prefer the highest eligible throughput score because too few independent FMA chains can underuse the vector pipelines.
- For `intrinsics`, do not select a pressure-ratio-near-1.0 shape by default; use `--max-spill-risk high` only when the generated assembly will be inspected and fallback candidates are available.
- Load/FMA scheduling should interleave future loads with current accumulator updates.
- For fixed small K, let `loop-unrolling` fully unroll the internal K loop only when register budget and code-size risk pass.
- Tail handling belongs outside the hot micro-kernel when possible: peel boundaries, then run a clean full-tile kernel.
- For SVE, keep the loop length-agnostic; do not hard-code NEON lane counts into SVE code.
- For SME ZA/tile, follow `docs/sme-za-tile-guide.md` and `docs/sme-za-inline-asm-guide.md`.

## JSON Output Guidance

When a micro-kernel shape is selected, fill optional fields:

```json
{
  "microkernel_shape": {
    "m": 20,
    "n": 8,
    "k_unroll": 1,
    "vector_lanes": 8,
    "accumulator_registers": 20
  },
  "register_budget": {
    "available_vector_registers": 26,
    "needed_vector_registers": 24,
    "spill_risk": "high",
    "register_class_budgets": {
      "vector": {"available": 26, "needed": 24, "spill_risk": "high"},
      "predicate": {"available": 14, "needed": 1, "spill_risk": "low"},
      "gpr": {"available": 18, "needed": 5, "spill_risk": "low"},
      "za_tile": {"available": 0, "needed": 0, "spill_risk": "low"}
    }
  },
  "spill_risk": "high",
  "register_allocation_plan": {
    "strategy": "maximize_verified_throughput_score",
    "selected_shape": "20x8",
    "max_spill_risk": "high",
    "verification_required": true,
    "verification_actions": [
      "compile selected kernel to assembly with benchmark target flags",
      "run register-pressure-analysis and scan for stack spills"
    ]
  },
  "candidate_register_allocations": [
    {
      "shape": "4x8",
      "accumulator_registers": 4,
      "underutilization_risk": true
    }
  ],
  "selected_register_allocation": {
    "shape": "20x8",
    "accumulator_registers": 20,
    "pressure_ratio": 0.923,
    "spill_risk": "high"
  },
  "fallback_register_allocations": [
    {
      "shape": "16x8",
      "accumulator_registers": 16,
      "pressure_ratio": 0.769,
      "spill_risk": "medium"
    }
  ],
  "underutilization_risk": false,
  "verification_required": true,
  "verification_actions": [
    "compile selected kernel to assembly with benchmark target flags",
    "run register-pressure-analysis and scan for stack spills"
  ]
}
```

If no micro-kernel is used, omit these fields or set them to `null`.

## Verification Checklist

- The selected shape is tied to dtype and ISA lane count.
- Register allocation candidates and the selected budget are recorded for GEMM/convolution/filter/stencil style kernels.
- Lower-accumulator candidates such as 4xVL are marked as underutilized when a larger candidate is selected.
- `fallback_register_allocations` is available when verification needs to reduce the selected shape.
- Generated code does not write partial accumulators to memory inside the inner accumulation loop.
- Assembly spill analysis is requested for SIMD, inline asm, standalone assembly, and any selected micro-kernel with `medium` or `high` spill risk.
