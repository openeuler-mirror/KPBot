# Reduction 决策指南

本文件说明 `apply-vectorization` 首版允许的 reduction 形态，以及必须拒绝的相近循环。

## 1. 支持范围

首版只支持：

- `sum`：`acc += x[i]`
- `dot`：`acc += a[i] * b[i]`

这两类 reduction 只有在 `acc` 不参与循环内输出、只作为最终返回值或循环后写出时才允许。输入访问必须是连续、unit-stride，循环体不能包含 I/O、锁、原子、不可证明安全的函数调用或冲突写。

## 2. 必须拒绝的相近形态

这些情况不属于首版 reduction：

- `out[i] = acc` 或类似 running accumulator 输出
- prefix scan / prefix sum
- histogram、scatter、gather 或输出冲突
- `min`、`max`、`product` 等尚未纳入首版范围的归约
- 混合类型或不清楚目标 ISA 映射的归约

## 3. 浮点规则

`float32` reduction 可以向量化为“向量累加器 + 最终水平归约”，但会改变标量左到右加法顺序。

因此成功结果必须在 `safety_checks` 中说明：

- 结果不保证 bit-exact
- 允许轻微舍入差异
- 如果 request 或 dependencies 要求严格顺序、bit-exact、可复现实验级精确顺序，则必须拒绝

## 4. 整型规则

`int32` reduction 只在不依赖溢出语义时允许。若源码、request 或 dependencies 暗示需要 checked overflow、trap、严格未定义行为复现，或者模型无法证明溢出语义不影响结果，则必须拒绝。

## 5. 架构映射

推荐形态：

- `NEON float32 sum`：`vaddq_f32` 累加，`vaddvq_f32` 最终水平归约，标量尾处理合并到最终结果
- `NEON int32 sum`：`vaddq_s32` 或等价 int32 向量累加，`vaddvq_s32` 最终水平归约
- `SVE float32 sum`：`svadd_f32_x` 或 `svmla_f32_x` 累加，`svaddv_f32` 最终归约
- `SVE int32 sum`：int32 SVE 累加，`svaddv_s32` 最终归约
- `dot`：使用向量乘加累加器；SVE2 int8 dot 可查 `svdot_s32`

`SME` 默认不为 sum/dot reduction 进入 `ZA/tile`。除非输入循环已经是 GEMM、rank-k 或 outer-product 风格二维块累加，否则使用 NEON、SVE 或 SME streaming-compatible 路径。
