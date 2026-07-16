## 8. 头文件快速参考

### 保护模式

```c
// SVE 代码
#ifdef __ARM_FEATURE_SVE
#include <arm_sve.h>
void my_sve_function(float *data, int n) {
    for (int i = 0; i < n; i += svcntw()) {
        svbool_t pg = svwhilelt_b32(i, n);
        svfloat32_t v = svld1_f32(pg, &data[i]);
        svst1_f32(pg, &data[i], svmul_f32_x(pg, v, v));
    }
}
#endif

// Neon 代码
#ifdef __ARM_NEON
#include <arm_neon.h>
void my_neon_function(float *data, int n) {
    for (int i = 0; i + 4 <= n; i += 4) {
        float32x4_t v = vld1q_f32(&data[i]);
        vst1q_f32(&data[i], vmulq_f32(v, v));
    }
}
#endif

// SME 代码
#ifdef __ARM_FEATURE_SME
#include <arm_sme.h>
__arm_locally_streaming
void my_sme_function(void) {
    // SME 内联函数需要流模式
    // __arm_locally_streaming 自动进入流模式
}
#endif
```

### SME 流模式属性

```c
// 函数内部在流模式下运行
__arm_locally_streaming void fn() { /* 可使用流模式内联函数 */ }

// 函数类型为流模式（调用者必须处于流模式）
void fn() __arm_streaming;

// 函数类型为流模式兼容（在任何模式下均可工作）
void fn() __arm_streaming_compatible;

// 运行时检查当前模式
if (__arm_in_streaming_mode()) { /* ... */ }
```

### SME ZA 状态管理

```c
// 函数从调用者读取 ZA 并写回修改后的 ZA
void fn() __arm_inout("za");

// 函数读取 ZA 但不修改它
void fn() __arm_in("za");

// 函数写入新的 ZA 内容（忽略传入的）
void fn() __arm_out("za");

// 函数保留 ZA 而不读取它
void fn() __arm_preserves("za");

// 函数创建一个新的 ZA 作用域（不与调用者共享）
__arm_new("za") void fn() __arm_inout("za") { /* 全新的 ZA */ }
```

---
