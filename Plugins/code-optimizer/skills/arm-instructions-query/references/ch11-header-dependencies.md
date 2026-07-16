## 11. 头文件包含顺序与依赖

包含 ACLE 头文件有**传递效应** — 头文件会自动包含其他头文件。
理解这一点可以避免重复包含和类型缺失错误。

### 包含依赖图

```
<arm_neon.h>
  ├─ <arm_fp16.h>     （如果 __ARM_FEATURE_FP16_SCALAR_ARITHMETIC）
  └─ <arm_bf16.h>     （如果 __ARM_FEATURE_BF16，可用时）

<arm_sve.h>
  ├─ <stdint.h>
  ├─ <stdbool.h>      （仅 C）
  ├─ <arm_fp16.h>
  └─ <arm_bf16.h>     （如果可用）

<arm_neon_sve_bridge.h>
  ├─ <arm_neon.h>
  └─ <arm_sve.h>

<arm_sme.h>
  └─ <arm_sve.h>

<arm_mve.h>
  （无自动包含，但需要 __ARM_FEATURE_MVE）
```

### 包含的最佳实践

```c
// ✅ 正确：按依赖顺序包含，由特性宏保护
#if defined(__ARM_FEATURE_SME)
#include <arm_sme.h>    // 自动拉入 arm_sve.h、stdint.h 等
#elif defined(__ARM_FEATURE_SVE)
#include <arm_sve.h>
#elif defined(__ARM_NEON)
#include <arm_neon.h>
#endif

// ✅ 正确：需要时显式包含标准头文件
#include <stdint.h>     // 用于 uint32_t 等
#include <stdbool.h>    // 用于 bool
#if defined(__ARM_FEATURE_SVE)
#include <arm_sve.h>
#endif

// ❌ 错误：依赖 ACLE 头文件拉入 <stdint.h>
// ACLE 头文件不定义 uint32_t 等标准类型
// 除非包含标准头文件 — 这是实现定义的
```

### `__ARM_NEON_SVE_BRIDGE` 保护

```c
// 包含 bridge 头文件之前，检查特性宏
#if defined(__ARM_NEON_SVE_BRIDGE) && __ARM_NEON_SVE_BRIDGE
#include <arm_neon_sve_bridge.h>
// 现在 svset_neonq、svget_neonq、svdup_neonq 可用
#endif
```

### MVE 特性检测（整数 vs. 整数+浮点）

```c
#if (__ARM_FEATURE_MVE & 3) == 3
#include <arm_mve.h>
/* 整数和浮点 MVE 内联函数可用 */
#elif __ARM_FEATURE_MVE & 1
#include <arm_mve.h>
/* 仅整数 MVE 内联函数可用 */
#else
/* 无 MVE 支持 */
#endif
```

### 避免命名空间冲突（MVE）

```c
// 如果你的代码有与 MVE 内联函数同名的标识符，
// 在包含 <arm_mve.h> 之前定义这个：
#define __ARM_MVE_PRESERVE_USER_NAMESPACE
#include <arm_mve.h>
// 现在 MVE 内联函数仅存在于 __arm_ 命名空间
```

### 特性宏与目标属性

**重要：** ACLE 特性宏反映的是**编译目标**，而不是特定函数上的目标属性。
不要假设 `target("+sve")` 属性会启用 `__ARM_FEATURE_SVE`：

```c
// ❌ 错误假设
__attribute__((target("+sve")))
void foo() {
#ifdef __ARM_FEATURE_SVE
    // 用户不应假设这个被定义！
    // 该宏反映的是默认目标，而不是每函数的属性。
#endif
}

// ✅ 正确：使用 target 属性为函数启用 SVE，
// 然后直接使用 SVE 内联函数（内部无宏检查）
__attribute__((target("+sve")))
void foo(float *data, int n) {
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        svst1_f32(pg, &data[i], svmul_f32_x(pg, v, v));
    }
}
```

---
