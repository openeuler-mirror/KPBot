/**
 * ARM (aarch64) 预取头文件 — 鲲鹏平台专用
 * 使用: #include "prefetch.h" 后直接调用 PREFETCH_READ / PREFETCH_NEXT 等宏
 *
 * 快速上手:
 *   #include "prefetch.h"
 *   for (int i = 0; i < n; i++) {
 *       PREFETCH_NEXT(&a[i], PREFETCH_DISTANCE);  // 预取 a[i+DIST]
 *       process(a[i]);
 *   }
 */

#ifndef PREFETCH_H
#define PREFETCH_H

#include <stddef.h>

// ================================================================
// 预取参数（可在编译时或代码中覆盖）
// ================================================================
#ifndef PREFETCH_DISTANCE
#define PREFETCH_DISTANCE 16    // 默认预取 16 个元素之后的数据
#endif

// ================================================================
// ARM (aarch64) 实现 — __builtin_prefetch 生成 prfm 指令
//
// 注意：ARM64 prfm 不能使用 [reg] 形式的通用寄存器寻址，
// 必须用 [base, index] 或 [base, #imm]。
// GCC 的 __builtin_prefetch(addr, rw, locality) 会自动生成
// 正确的 prfm PLDLxKEEP, [base, index] 形式，绝不要手写内联 asm。
//
// locality 取值（rw=0 读）:
//   0 = non-temporal，数据用完即 evict（适合一次性使用，如距离计算）
//   1 = 保持到 L3
//   2 = 保持到 L2
//   3 = 保持到所有缓存（适合被多次访问的数据）
//
// locality 取值（rw=1 写）:
//   0 = non-temporal
//   3 = 预取到 L1 准备写入
// ================================================================
#if defined(__aarch64__)

// locality=0: non-temporal，数据不进 L1，用完即 evict（适合一次性距离计算）
// locality=3: 预取到 L1 并保持（适合 visited 数组等被多次访问的数据）
#define PREFETCH_READ(ptr)       __builtin_prefetch(ptr, 0, 3)  // 预取到 L1 并保持：多次访问
#define PREFETCH_READ_NT(ptr)    __builtin_prefetch(ptr, 0, 0)  // non-temporal：一次性使用
#define PREFETCH_READ_L2(ptr)    __builtin_prefetch(ptr, 0, 2)  // 预取到 L2，不进 L1
#define PREFETCH_READ_L3(ptr)    __builtin_prefetch(ptr, 0, 1)  // 预取到 L3
#define PREFETCH_WRITE(ptr)      __builtin_prefetch(ptr, 1, 3)  // 预取准备写入

// ================================================================
// Fallback（非 ARM 平台，编译通过但无效果）
// ================================================================
#else
#define PREFETCH_READ(ptr)       ((void)0)
#define PREFETCH_READ_NT(ptr)   ((void)0)
#define PREFETCH_READ_L2(ptr)   ((void)0)
#define PREFETCH_READ_L3(ptr)   ((void)0)
#define PREFETCH_WRITE(ptr)     ((void)0)
#endif

// ================================================================
// 便捷宏
// ================================================================

/**
 * PREFETCH_NEXT — 在循环中预取 ahead 个元素之后的数据
 *
 * 用法:
 *   const int DIST = PREFETCH_DISTANCE;   // 或自定义常量
 *   for (int i = 0; i < n; i++) {
 *       PREFETCH_NEXT(&a[i], DIST);      // 预取 a[i+DIST]
 *       compute(a[i]);
 *   }
 *
 * 原理:
 *   &a[i] + DIST 等价于 &a[i + DIST]
 *   sizeof(*ptr) 为元素大小，由编译器自动推断
 */
#define PREFETCH_NEXT(ptr, ahead) do {        \
    __typeof__(ptr) _p_ = (ptr);              \
    char *_ap_ = (char *)_p_ + (ahead) * sizeof(*_p_); \
    PREFETCH_READ(_ap_);                     \
} while (0)

/**
 * PREFETCH_PREV — 预取当前元素之前的 DIST 个元素（逆向遍历）
 */
#define PREFETCH_PREV(ptr, behind) do {       \
    __typeof__(ptr) _p_ = (ptr);              \
    char *_ap_ = (char *)_p_ - (behind) * sizeof(*_p_); \
    PREFETCH_READ(_ap_);                     \
} while (0)

/**
 * PREFETCH_ARRAY — 预取整个数组的后 DIST 个元素（适合初始化场景）
 *
 * 用法:
 *   PREFETCH_ARRAY(arr, n, DIST);   // 预取 arr[n-1 .. n-DIST]
 *   for (int i = 0; i < n; i++) { process(arr[i]); }
 */
#define PREFETCH_ARRAY(arr, n, ahead) do {             \
    size_t _n_ = (n);                                  \
    size_t _a_ = (ahead);                              \
    if (_a_ < _n_) {                                   \
        __typeof__(arr) _arr_ = (arr);                  \
        PREFETCH_READ(&_arr_[_n_ - _a_]);               \
    }                                                  \
} while (0)

// ================================================================
// 平台检测宏（供用户代码判断）
// ================================================================
#if defined(__aarch64__)
    #define PREFETCH_PLATFORM "aarch64"
#else
    #define PREFETCH_PLATFORM "unknown"
#endif

#endif  // PREFETCH_H
