## 5. 特性检测宏

### 架构检测

| 宏 | 含义 |
|----|------|
| `__ARM_ARCH` | 架构版本（如 8 表示 Armv8，9 表示 Armv9） |
| `__ARM_PROFILE` | 配置文件：`'A'`（应用）、`'R'`（实时）、`'M'`（微控制器） |
| `__ARM_64BIT_STATE` | 编译为 AArch64 时为 1 |
| `__ARM_32BIT_STATE` | 编译为 AArch32 时为 1 |

### 向量扩展检测

| 宏 | 含义 |
|----|------|
| `__ARM_NEON` | Neon SIMD 可用（定义时始终为 1） |
| `__ARM_FEATURE_SVE` | SVE 可用 |
| `__ARM_FEATURE_SVE2` | SVE2 可用 |
| `__ARM_FEATURE_SVE_BITS` | 固定 SVE 向量长度（可选，不固定时为 0） |
| `__ARM_FEATURE_SME` | SME 可用 |
| `__ARM_FEATURE_SME2` | SME2 可用 |
| `__ARM_FEATURE_MVE` | M 系列向量扩展（1=整数，3=整数+浮点） |

### 特定特性检测

| 宏 | 特性 |
|----|------|
| `__ARM_FEATURE_DOTPROD` | 点积（SDOT/UDOT） |
| `__ARM_FEATURE_MATMUL_INT8` | 整数矩阵乘法（SMMLA/UMMLA） |
| `__ARM_FEATURE_BF16` | BFloat16 向量指令 |
| `__ARM_FEATURE_FP16_FML` | FP16 融合乘加（长） |
| `__ARM_FEATURE_COMPLEX` | 复数指令 |
| `__ARM_FEATURE_FMA` | 融合乘累加 |
| `__ARM_FEATURE_CRYPTO` | 加密扩展（AES+SHA） |
| `__ARM_FEATURE_AES` | AES 指令 |
| `__ARM_FEATURE_SHA2` | SHA-256 指令 |
| `__ARM_FEATURE_CRC32` | CRC32 指令 |
| `__ARM_FEATURE_MEMORY_TAGGING` | 内存标记扩展（MTE） |
| `__ARM_FEATURE_SVE_BF16` | SVE BFloat16 支持 |
| `__ARM_FEATURE_SVE_B16B16` | SVE 非加宽 BFloat16 |
| `__ARM_FEATURE_SME_F64F64` | SME 双精度外积 |
| `__ARM_FEATURE_SME_I16I64` | SME 16→64 位加宽外积 |

### 条件编译模式

```c
#if defined(__ARM_FEATURE_SVE2)
    // SVE2 优化路径
    result = compute_sve2(data, n);
#elif defined(__ARM_FEATURE_SVE)
    // SVE 路径
    result = compute_sve(data, n);
#elif defined(__ARM_NEON)
    // Neon 路径
    result = compute_neon(data, n);
#else
    // 标量回退
    result = compute_scalar(data, n);
#endif
```

---
