## 12. SME 流模式 — 深入探讨

流模式（PSTATE.SM）从根本上改变了 SVE 向量的行为以及哪些指令可用。
误解它会导致微妙的错误。

### 流模式的三个主要影响

1. **向量长度改变**：流向量长度（SVL）可能与非流式 SVE VL 不同
2. **仅限流模式指令**：某些 SME 内联函数（ZA 外积、块操作）需要流模式
3. **非流模式指令被阻止**：某些 SVE2 指令不能在流模式下执行

### 属性决策指南

| 属性 | 使用场景 |
|------|----------|
| `__arm_locally_streaming` | 内部实现细节 — 使用 SME 而不向调用者暴露 |
| `__arm_streaming` | 需要流模式的公共 API（调用者必须切换） |
| `__arm_streaming_compatible` | 在任一模式下均可工作的长度无关 SVE 代码 |

### 性能考虑：模式切换开销

```c
// ❌ 不好：每次循环迭代都切换模式
for (int i = 0; i < n; i++) {
    sme_inner_function(&data[i]);  // 每次都进入 + 退出流模式
}

// ✅ 好：整个循环保持在流模式中
__arm_locally_streaming void process_all(float *data, int n) {
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        // SME 内联函数在这里可用 — 无模式切换
    }
}
```

### 流兼容函数

```c
// 在任一模式下均可工作的长度无关 SVE 代码
__arm_streaming_compatible
float sum_compatible(const float *data, int n) {
    svfloat32_t acc = svdup_f32(0.0f);
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        acc = svadd_f32_x(pg, acc, v);
    }
    return svaddv_f32(svptrue_b32(), acc);
}
// 此函数可以从流模式或非流模式上下文调用
// 而没有任何模式切换开销。
```

### ZA 状态管理模式

```c
// 常见模式：初始化 ZA，计算，提取结果
__arm_new("za") __arm_locally_streaming
void matmul_sme(const float *A, const float *B, float *C, int M, int N, int K) {
    // 将 ZA 块初始化为零
    svzero_za();

    // 外积累加
    for (int k = 0; k < K; k += svcntw()) {
        svbool_t pg = svwhilelt_b32(k, K);
        svfloat32_t a_col = svld1_f32(pg, &A[k]);  // A 的列
        svfloat32_t b_row = svld1_f32(pg, &B[k]);  // B 的行
        // 外积累加到 ZA 块
        svmopa_za(pg, pg, a_col, b_row);
    }

    // 将 ZA 块提取回内存
    // ...（将 ZA 行存储到 C）
}
```
