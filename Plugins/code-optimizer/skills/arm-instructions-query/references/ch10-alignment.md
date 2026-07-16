## 10. 对齐注意事项

对齐对向量化代码很重要 — 未对齐的访问可能导致故障（AArch32）
或严重的性能损失（AArch64）。

### 查询对齐限制

```c
#include <arm_acle.h>

// 最大静态数据对齐（2 的幂指数）
// 例如 4 表示 1<<4 = 16 字节
int max_static = __ARM_ALIGN_MAX_PWR;

// 最大栈对齐（2 的幂指数）
// 例如 3 表示 1<<3 = 8 字节（AArch32），4 表示 16 字节（AArch64）
int max_stack = __ARM_ALIGN_MAX_STACK_PWR;
```

### 栈对齐保证

| 架构 | 保证的栈对齐 |
|------|-------------|
| AArch32 | 8 字节 |
| AArch64 | 16 字节 |

```c
// 为 Neon 加载/存储对齐栈缓冲区
int32x4_t process_neon(void) {
    alignas(16) int32_t buffer[4] = {1, 2, 3, 4};  // 16 字节对齐
    return vld1q_s32(buffer);  // 安全：缓冲区已对齐
}
```

### 堆对齐

标准的 `malloc()` 不保证向量化代码所需的充分对齐。使用显式对齐分配：

```c
#include <stdlib.h>

// C11 aligned_alloc（可用时优先使用）
float *buf = aligned_alloc(64, n * sizeof(float));  // 64 字节对齐

// POSIX 替代方案
float *buf;
posix_memalign((void**)&buf, 64, n * sizeof(float));

// 在向量化循环中使用
for (int i = 0; i < n; i += svcntw()) {
    svbool_t pg = svwhilelt_b32(i, n);
    svfloat32_t v = svld1_f32(pg, &buf[i]);  // 对齐数据效率高
    // ...
}
```

### 对齐最佳实践

1. **栈缓冲区**：Neon 使用 `alignas(16)`，SVE 使用 `alignas(VL_BYTES)`
   （其中 VL_BYTES 是运行时的向量长度，以字节为单位）
2. **堆缓冲区**：使用 `aligned_alloc`，对齐 ≥ 缓存行大小（通常 64 字节）
3. **结构体成员**：对向量成员使用 `__attribute__((aligned(N)))`
4. **不要过度对齐**：过度对齐的栈对象在某些实现上可能被视为提示
5. **对于 SVE**：当向量长度未知时，超量分配 `2 * MAX_VL_BYTES`
   （通常 256 字节）以保证至少一个对齐的向量

---
