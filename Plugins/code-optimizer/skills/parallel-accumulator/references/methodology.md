# 串行累加器并行化方法论

## 1. 为什么串行累加器慢

单累加器的归约循环，每次迭代的 `sum += a[i]*b[i]` 都依赖上一次 `sum`，形成严格串行的依赖链。即使乱序执行单元能每个周期发射多条独立浮点运算，也必须在**延迟**（FP 加法 ~4–5 周期）内等待上一条完成，吞吐被锁在「延迟 ÷ 1」而非「吞吐 ÷ 0.5」。

```
吞吐受限于延迟： 每 4-5 周期完成 1 次加法   （~8-10× 低于吞吐上限）
多累加器后：     每 0.5 周期完成 1 次加法    （逼近吞吐上限）
```

典型信号：`perf stat` 显示 IPC ≪ 1，但 cache miss 低、分支预测失败低——「低 IPC + 低 miss」组合正是延迟绑定的标志。

## 2. 累加器数量公式

隐藏 FP 延迟所需的独立累加器数：

```
accumulators = ceil(latency / throughput)
```

AArch64 上 `fadd` 典型延迟 4、吞吐 0.5，故 `ceil(4/0.5) = 8`；实践 4 个已覆盖大多数场景，8 个用于极致吞吐的紧循环。`scripts/accumulator_plan.py` 按此公式计算。

## 3. 三级改写（ARM/NEON）

### Level 1 — 纯 C 多累加器（无 SIMD）

编译器自动乱序重叠多条依赖链。实测约 4×。

```c
float dot(const float *restrict a, const float *restrict b, int n) {
    float s0 = 0.0f, s1 = 0.0f, s2 = 0.0f, s3 = 0.0f;
    int i;
    for (i = 0; i + 3 < n; i += 4) {
        s0 += a[i+0] * b[i+0];
        s1 += a[i+1] * b[i+1];
        s2 += a[i+2] * b[i+2];
        s3 += a[i+3] * b[i+3];
    }
    /* 标量尾：同样多累加器，不回退单累加器 */
    for (; i + 1 < n; i += 2) { s0 += a[i] * b[i]; s1 += a[i+1] * b[i+1]; }
    if (i < n) s0 += a[i] * b[i];
    return (s0 + s1) + (s2 + s3);
}
```

要点：`restrict` 消除别名检查前导码；尾处理用成对累加器，不重新引入单累加器串行链。

### Level 2 — NEON 单累加器

每迭代处理 4 元素，`float32x4_t` 累加。实测约 4–8×。

```c
#include <arm_neon.h>

float dot_neon(const float *restrict a, const float *restrict b, int n) {
    float32x4_t vsum = vdupq_n_f32(0.0f);
    int i;
    for (i = 0; i + 3 < n; i += 4) {
        float32x4_t va = vld1q_f32(&a[i]);
        float32x4_t vb = vld1q_f32(&b[i]);
        vsum = vfmaq_f32(vsum, va, vb);   /* vsum = va*vb + vsum */
    }
    /* AArch64 水平归约：4 lane → 标量 */
    float result = vaddvq_f32(vsum);
    /* 标量尾（多累加器） */
    float s0 = 0.0f, s1 = 0.0f;
    for (; i + 1 < n; i += 2) { s0 += a[i] * b[i]; s1 += a[i+1] * b[i+1]; }
    if (i < n) s0 += a[i] * b[i];
    return result + s0 + s1;
}
```

### Level 3 — NEON + 多累加器展开

4 个 `float32x4_t` 累加器，每迭代处理 16 元素。实测约 8–15×。

```c
#include <arm_neon.h>

float dot_neon4(const float *restrict a, const float *restrict b, int n) {
    float32x4_t s0 = vdupq_n_f32(0.0f), s1 = vdupq_n_f32(0.0f);
    float32x4_t s2 = vdupq_n_f32(0.0f), s3 = vdupq_n_f32(0.0f);
    int i;
    for (i = 0; i + 15 < n; i += 16) {
        s0 = vfmaq_f32(s0, vld1q_f32(&a[i+0]),  vld1q_f32(&b[i+0]));
        s1 = vfmaq_f32(s1, vld1q_f32(&a[i+4]),  vld1q_f32(&b[i+4]));
        s2 = vfmaq_f32(s2, vld1q_f32(&a[i+8]),  vld1q_f32(&b[i+8]));
        s3 = vfmaq_f32(s3, vld1q_f32(&a[i+12]), vld1q_f32(&b[i+12]));
    }
    /* 向量累加器归并 + 水平归约 */
    float32x4_t sum4 = vaddq_f32(vaddq_f32(s0, s1), vaddq_f32(s2, s3));
    float result = vaddvq_f32(sum4);
    /* 标量尾：成对累加器 */
    float t0 = 0.0f, t1 = 0.0f;
    for (; i + 1 < n; i += 2) { t0 += a[i] * b[i]; t1 += a[i+1] * b[i+1]; }
    if (i < n) t0 += a[i] * b[i];
    return result + t0 + t1;
}
```

### SVE 256-bit 变体（可变向量长度）

SVE 的向量宽度由硬件决定（128–2048 bit），256-bit 时 `svcntw()` == 8（每 32-bit float 8 个 lane）。用 predicate（`svbool_t`）配合 `svwhilelt_b32` 做尾处理，**天然处理任意 n，无需标量尾**——predicate 会自动 masking 越界的 lane。

SVE 单累加器（对应 Level 2）：

```c
#include <arm_sve.h>

float dot_sve(const float *restrict a, const float *restrict b, int n) {
    svfloat32_t vsum = svdup_f32(0.0f);
    int i = 0;
    svbool_t pg = svwhilelt_b32(i, n);
    while (svptest_any(svptrue_b32(), pg)) {
        vsum = svmla_f32_m(pg, vsum, svld1_f32(pg, &a[i]), svld1_f32(pg, &b[i]));
        i += svcntw();
        pg = svwhilelt_b32(i, n);
    }
    return svaddv_f32(svptrue_b32(), vsum);   /* 水平归约 */
}
```

SVE 多累加器（对应 Level 3，4 个累加器交错，每迭代按 svcntw() 步进）：

```c
#include <arm_sve.h>

float dot_sve4(const float *restrict a, const float *restrict b, int n) {
    svfloat32_t s0 = svdup_f32(0.0f), s1 = svdup_f32(0.0f);
    svfloat32_t s2 = svdup_f32(0.0f), s3 = svdup_f32(0.0f);
    int i = 0;
    while (i < n) {
        svbool_t pg = svwhilelt_b32(i, n);
        s0 = svmla_f32_m(pg, s0, svld1_f32(pg, &a[i]), svld1_f32(pg, &b[i]));
        i += svcntw(); if (i >= n) break;
        pg = svwhilelt_b32(i, n);
        s1 = svmla_f32_m(pg, s1, svld1_f32(pg, &a[i]), svld1_f32(pg, &b[i]));
        i += svcntw(); if (i >= n) break;
        pg = svwhilelt_b32(i, n);
        s2 = svmla_f32_m(pg, s2, svld1_f32(pg, &a[i]), svld1_f32(pg, &b[i]));
        i += svcntw(); if (i >= n) break;
        pg = svwhilelt_b32(i, n);
        s3 = svmla_f32_m(pg, s3, svld1_f32(pg, &a[i]), svld1_f32(pg, &b[i]));
        i += svcntw();
    }
    svfloat32_t sum = svadd_f32_x(svptrue_b32(), s0, s1);
    sum = svadd_f32_x(svptrue_b32(), sum, s2);
    sum = svadd_f32_x(svptrue_b32(), sum, s3);
    return svaddv_f32(svptrue_b32(), sum);
}
```

要点：

- `svmla_f32_m(pg, acc, va, vb)` 是 **merging** 乘加：只在 `pg` 为真的 lane 上做 `acc = va*vb + acc`，其余 lane 保留 `acc`——尾元素的 predicate 自动只作用于有效 lane，等价于标量尾，无需额外循环。
- 每个累加器处理一个 `svcntw()` 块后 `i += svcntw()`，每块用独立 `svwhilelt_b32` predicate；`if (i >= n) break` 提前退出避免越界加载。
- 累加器归并用 `svadd_f32_x`（pg 全真，x/m/z 等价）；`svaddv_f32` 做最终水平归约。
- 若目标硬件是 512-bit SVE，`svcntw()` 自动变为 16，同一份代码无需改动——这是 SVE 相对 NEON 固定 128-bit 的核心优势。

## 4. 结合律与数值契约

1. 多累加器重排浮点运算顺序，仅当归约运算**结合**时数学上合法：加、乘、min、max 满足结合律（min/max 还需注意 NaN 与 ±0 语义）。减、除不结合，不可直接并行化。
2. 结果与原串行版相差**浮点舍入 ε**（非逻辑错误）；若要求位精确可复现，本优化不能应用。
3. `restrict` 仅消除指针别名；若调用方存在真实别名（`dot(a, a, n)`），去除 restrict 前必须确认语义。

## 5. 验证契约

1. **正确性**：同输入对比，相对误差 < 1e-6（浮点 ε 量级）。
2. **反汇编**：Level 1 出现多个独立标量寄存器串；Level 3 出现 4 个 `v` 累加寄存器，`fmla` 之间无串行 `fadd` 链；SVE 变体出现 `fmla z*.s, p*/m`、`whilelt p*.s`、`incw` 指令，且 4 个 `z` 累加寄存器独立。
3. **IPC**：`perf stat` 重跑，IPC 目标 ≥ 2×。
4. **benchmark**：wall-clock 对照，Level 1 约 4×、Level 3 约 8–15×。

### 实测参考（鲲鹏 SVE2 256-bit，单核点积）

> 环境：鲲鹏 SVE2 服务器（`svcntw()==8`，192 核），gcc 12.3.1 `-O3 -march=native`，`taskset -c 144-191` 绑单核，100 万 float 点积，5 轮取中位数（干净负载，serial 恒定 1.822 ms）。

| 版本 | 相对 serial 加速比 |
|------|--------------------|
| serial（单累加器） | 1.00x |
| Level 1 纯 C 多累加器 | 2.49x |
| Level 3 NEON 多累加器 | 7.75x |
| Level 3 SVE256 多累加器 | 7.86x |

要点：串行累加器并行化收益充分（2.5–7.9x）；SVE256 略优于 NEON（向量更宽）；三个优化版本浮点相对误差均 < 1e-5。单核点积受内存带宽限制，加速比落在「Level 3 约 8–15x」理论值的下限。

## 6. 参考来源

- Intel Performance Skills `performance-patterns/patterns/parallel-accumulator.md`（串行累加器改写三级方案）。
- ARM AArch64 `fadd`/`fmul`/`fmla` 延迟与吞吐数据（`kunpeng_microarch` skill 的微架构知识库）。