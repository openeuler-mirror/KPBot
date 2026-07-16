## 1. 快速参考卡

### 头文件

| 头文件 | 包含条件 | 提供内容 |
|--------|----------|----------|
| `<arm_neon.h>` | `__ARM_NEON == 1` | Neon SIMD 内联函数（128 位固定向量） |
| `<arm_sve.h>` | `__ARM_FEATURE_SVE` | SVE 内联函数（可扩展向量） |
| `<arm_sme.h>` | `__ARM_FEATURE_SME` | SME 内联函数（矩阵块、流模式） |
| `<arm_mve.h>` | `__ARM_FEATURE_MVE` | M 系列向量扩展（Helium） |
| `<arm_neon_sve_bridge.h>` | `__ARM_NEON_SVE_BRIDGE` | NEON↔SVE 转换内联函数 |
| `<arm_acle.h>` | 始终包含 | DSP、屏障、预取、CRC32、MTE |
| `<arm_fp16.h>` | `__ARM_FP16_FORMAT_IEEE` | FP16 标量内联函数 |
| `<arm_bf16.h>` | `__ARM_BF16_FORMAT_ALTERNATIVE` | BFloat16 标量内联函数 |

### 类型命名约定

```
Neon:  <base>x<count>_t       int32x4_t   （4 × int32 = 128 位）
SVE:   sv<base>_t             svint32_t   （长度无关的 int32 向量）
SVE:   sv<base>x<N>_t         svint32x2_t （2 个 SVE 向量的元组）
MVE:   <base>x<count>_t       int32x4_t   （与 Neon 命名相同）
Pred:  svbool_t               svbool_t    （SVE 谓词，每字节一位）
```

基本类型：`int8/16/32/64`、`uint8/16/32/64`、`float16/32/64`、`bfloat16`、`poly8/16/64`

### 条件编译检查清单

```c
#if defined(__ARM_FEATURE_SVE)        // SVE 可用
#if defined(__ARM_FEATURE_SVE2)       // SVE2 可用
#if defined(__ARM_FEATURE_SME)        // SME 可用
#if defined(__ARM_NEON)               // Neon 可用（定义时始终为 1）
#if defined(__ARM_FEATURE_DOTPROD)    // 点积指令
#if defined(__ARM_FEATURE_MATMUL_INT8)// 整数矩阵乘法
#if defined(__ARM_FEATURE_BF16)       // BFloat16 向量支持
```

---
