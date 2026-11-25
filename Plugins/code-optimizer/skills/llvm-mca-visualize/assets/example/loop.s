// SAXPY-style NEON loop body for llvm-mca pipeline visualization
// y[i] = a*x[i] + y[i]
.Lloop:
    ldr  q0, [x0], #16
    ldr  q1, [x1], #16
    fmul v2.4s, v0.4s, v3.4s
    fadd v4.4s, v2.4s, v1.4s
    str  q4, [x2], #16
    ldr  q5, [x0], #16
    ldr  q6, [x1], #16
    fmul v7.4s, v5.4s, v3.4s
    fadd v8.4s, v7.4s, v6.4s
    str  q8, [x2], #16
