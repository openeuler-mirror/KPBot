# C/C++ 路径循环展开

本文件为 `loop-unrolling` Skill 的 C/C++ 展开路径参考，处理 `c_cpp` 语言类型。

## 步骤 1B：分析 C/C++ 循环结构

1. 用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的函数代码

2. **自动检测代码类型**（即使输入中指定了 `simd_type`，也应从源码验证）：
   - 检测 NEON / SVE / SME intrinsics → 确定 simd_type
   - 无 intrinsics → `scalar`

3. **识别循环结构**：
   - **for 循环**：提取初始化（`int i = 0`）、条件（`i < n`）、增量（`i += step`）
   - **while 循环**：识别循环变量和退出条件
   - **嵌套循环**：区分外层和内层，展开目标为最内层循环

4. **判断循环体复杂度**：
   - **可展开**：循环体仅含算术/赋值/数组访问，无条件分支和函数调用
   - **不可展开**：循环体含 `if`/`break`/`continue`/函数调用 → 返回 `success=false`
   - 含简单条件赋值（如 `a[i] = cond ? x : y`）→ 可展开，条件部分保留

5. **分析数据访问模式**：
   - **连续访问**：`a[i]`, `a[i+1]` — 展开后用偏移量递增
   - **跨步访问**：`a[i*stride]` — 展开后步进乘 stride
   - **间接访问**：`a[index[i]]` — 可展开但各份访存不连续

6. **分析并行度**：统计循环体内独立的操作数量，记为 `current_parallelism`

7. **分析数据依赖**：
   - **累加型依赖**（如 `sum += a[i] * b[i]` 或 `sum_vec = vmlaq_f32(sum_vec, ...)`）→ 可拆分累加器
   - **循环携带依赖**（迭代 i 结果被 i+1 使用，如 `a[i] = a[i-1] + b[i]`）→ **不可展开**，返回 `success=false`

## 步骤 3B：生成 C/C++ 展开代码

### 3B-1. 展开循环体

按 `unroll_factor` 复制循环体 N 份：

1. **独立变量分配**：每份展开体使用独立的临时变量（如 `va0/vb0/vc0`, `va1/vb1/vc1`, ...）
2. **偏移计算**：每份使用 `i + offset` 访问数据
   - 标量：每份步进 1 元素
   - NEON float32：每份步进 4 元素
   - NEON float64：每份步进 2 元素
   - SVE：每份步进 `svcntw()` 元素
3. **累加器拆分**（归约类操作）：
   - 标量：`sum += ...` → `sum0 += ...; sum1 += ...; ...`，最后 `sum = sum0 + sum1 + ...`
   - SIMD：`sum_vec = vmlaq_f32(sum_vec, ...)` → 多个独立累加器，最后 `vaddq_f32` 合并

### 3B-2. 调整循环结构

1. 主循环步长调整为原始步长的 `unroll_factor` 倍
2. 添加余数处理：
   - 标量：标量余数循环
   - NEON：向量化余数循环 + 标量余数循环
   - SVE：谓词自然处理
3. 嵌套循环仅展开最内层，外层循环结构不变

### 3B-3. 展开示例

**标量归约展开**（unroll_factor=4）：

```c
// 原始
float sum = 0.0f;
for (int i = 0; i < n; i++) {
    sum += a[i] * b[i];
}

// 展开后
float sum0 = 0.0f, sum1 = 0.0f, sum2 = 0.0f, sum3 = 0.0f;
int i = 0;
int n_unroll = n - (n % 4);
for (; i < n_unroll; i += 4) {
    sum0 += a[i]   * b[i];
    sum1 += a[i+1] * b[i+1];
    sum2 += a[i+2] * b[i+2];
    sum3 += a[i+3] * b[i+3];
}
float sum = sum0 + sum1 + sum2 + sum3;
for (; i < n; i++) {
    sum += a[i] * b[i];
}
```

**标量非归约展开**（unroll_factor=4）：

```c
// 原始
for (int i = 0; i < n; i++) {
    a[i] = b[i] + c[i];
}

// 展开后
int i = 0;
int n_unroll = n - (n % 4);
for (; i < n_unroll; i += 4) {
    a[i]   = b[i]   + c[i];
    a[i+1] = b[i+1] + c[i+1];
    a[i+2] = b[i+2] + c[i+2];
    a[i+3] = b[i+3] + c[i+3];
}
for (; i < n; i++) {
    a[i] = b[i] + c[i];
}
```

**NEON 归约展开**（unroll_factor=2，累加器拆分）：

```c
// 原始
float32x4_t sum_vec = vdupq_n_f32(0.0f);
for (int i = 0; i < n_vec; i += 4) {
    float32x4_t va = vld1q_f32(a + i);
    float32x4_t vb = vld1q_f32(b + i);
    sum_vec = vmlaq_f32(sum_vec, va, vb);
}

// 展开后（2 个独立累加器）
float32x4_t sum0 = vdupq_n_f32(0.0f);
float32x4_t sum1 = vdupq_n_f32(0.0f);
int i = 0;
int n_unroll = n_vec - (n_vec % 8);
for (; i < n_unroll; i += 8) {
    float32x4_t a0 = vld1q_f32(a + i);
    float32x4_t b0 = vld1q_f32(b + i);
    sum0 = vmlaq_f32(sum0, a0, b0);

    float32x4_t a1 = vld1q_f32(a + i + 4);
    float32x4_t b1 = vld1q_f32(b + i + 4);
    sum1 = vmlaq_f32(sum1, a1, b1);
}
// 合并累加器
sum_vec = vaddq_f32(sum0, sum1);
// 向量化余数
for (; i < n_vec; i += 4) {
    float32x4_t va = vld1q_f32(a + i);
    float32x4_t vb = vld1q_f32(b + i);
    sum_vec = vmlaq_f32(sum_vec, va, vb);
}
```

**NEON 非归约展开**（unroll_factor=4）：

```c
// 原始
for (int i = 0; i < n_vec; i += 4) {
    float32x4_t vb = vld1q_f32(b + i);
    float32x4_t vc = vld1q_f32(c + i);
    vst1q_f32(a + i, vaddq_f32(vb, vc));
}

// 展开后（4 份独立变量）
int i = 0;
int n_unroll = n_vec - (n_vec % 16);
for (; i < n_unroll; i += 16) {
    float32x4_t vb0 = vld1q_f32(b + i);
    float32x4_t vc0 = vld1q_f32(c + i);
    vst1q_f32(a + i, vaddq_f32(vb0, vc0));

    float32x4_t vb1 = vld1q_f32(b + i + 4);
    float32x4_t vc1 = vld1q_f32(c + i + 4);
    vst1q_f32(a + i + 4, vaddq_f32(vb1, vc1));

    float32x4_t vb2 = vld1q_f32(b + i + 8);
    float32x4_t vc2 = vld1q_f32(c + i + 8);
    vst1q_f32(a + i + 8, vaddq_f32(vb2, vc2));

    float32x4_t vb3 = vld1q_f32(b + i + 12);
    float32x4_t vc3 = vld1q_f32(c + i + 12);
    vst1q_f32(a + i + 12, vaddq_f32(vb3, vc3));
}
// 向量化余数
for (; i < n_vec; i += 4) {
    float32x4_t vb = vld1q_f32(b + i);
    float32x4_t vc = vld1q_f32(c + i);
    vst1q_f32(a + i, vaddq_f32(vb, vc));
}
// 标量余数
for (; i < n; i++) {
    a[i] = b[i] + c[i];
}
```

**SVE 归约展开**（unroll_factor=2，谓词尾处理）：

```c
// 原始
#include <arm_sve.h>

svfloat32_t sum_vec = svdup_n_f32(0.0f);
int64_t i = 0;
int64_t vl = svcntw();
for (; i + vl <= n; i += vl) {
    svbool_t pg = svwhilelt_b32_s64(i, n);
    svfloat32_t va = svld1_f32(pg, a + i);
    svfloat32_t vb = svld1_f32(pg, b + i);
    sum_vec = svmla_f32_m(pg, sum_vec, va, vb);
}

// 展开后（2 个独立累加器）
svfloat32_t sum0 = svdup_n_f32(0.0f);
svfloat32_t sum1 = svdup_n_f32(0.0f);
int64_t i = 0;
int64_t vl = svcntw();
int64_t vl2 = vl * 2;
int64_t n_unroll = n - (n % vl2);
for (; i < n_unroll; i += vl2) {
    svbool_t pg0 = svwhilelt_b32_s64(i, n);
    svfloat32_t va0 = svld1_f32(pg0, a + i);
    svfloat32_t vb0 = svld1_f32(pg0, b + i);
    sum0 = svmla_f32_m(pg0, sum0, va0, vb0);

    svbool_t pg1 = svwhilelt_b32_s64(i + vl, n);
    svfloat32_t va1 = svld1_f32(pg1, a + i + vl);
    svfloat32_t vb1 = svld1_f32(pg1, b + i + vl);
    sum1 = svmla_f32_m(pg1, sum1, va1, vb1);
}
// 合并累加器
sum_vec = svadd_f32_x(svptrue_b32(), sum0, sum1);
// SVE 谓词余数
for (; i < n; i += vl) {
    svbool_t pg = svwhilelt_b32_s64(i, n);
    svfloat32_t va = svld1_f32(pg, a + i);
    svfloat32_t vb = svld1_f32(pg, b + i);
    sum_vec = svmla_f32_m(pg, sum_vec, va, vb);
}
```
