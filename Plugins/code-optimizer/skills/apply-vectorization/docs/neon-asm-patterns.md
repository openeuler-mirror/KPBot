# NEON 汇编模板

## 背景

对于关键路径，汇编优化可达最优性能。本文档提供 NEON 内联汇编模板，参考 eval_framework 多项目（dav1d、x264、ComputeLibrary）的汇编实践。

## 汇编 vs Intrinsics

| 方面 | Intrinsics | 汇编 |
|------|-----------|------|
| 可读性 | 高 | 低 |
| 可移植性 | 编译器保证 | 手动管理 |
| 性能 | 接近最优 | 最优 |
| 寄存器分配 | 编译器决定 | 完全控制 |
| 调试难度 | 低 | 高 |
| 维护成本 | 低 | 高 |

**推荐**: 默认使用 intrinsics，关键算子可选汇编优化。

## codegen_style 自动选择

`codegen_style=auto` 下，NEON 的默认选择是：

- C/C++ 标量循环：生成 `intrinsics`
- C/C++ 中已有 `asm/__asm__`：生成或改进 `inline_asm`
- `.S/.s/.asm`：生成 `assembly` artifacts

不要因为源码已经含 inline asm 或 standalone assembly 就直接退出。先判断是否能证明约束、clobber、尾处理、符号和 ABI；只有无法证明时才拒绝。

`inline_asm` 必须检查：

- `asm volatile` 的输入输出约束正确
- clobber 包含 `"memory"` 和所有被写寄存器
- 主循环有显式尾处理
- 不混入另一套 ISA 模板

`assembly` artifacts 必须检查：

- `.text`、`.globl`、函数 label 和 `ret`
- AAPCS64 参数寄存器和返回值约定
- 不破坏 callee-saved 寄存器，或正确保存恢复
- 不把 `x18/w18` 当普通临时寄存器；Darwin/Apple arm64 将它作为平台保留寄存器
- wrapper 与 `.S` 暴露符号一致

## GNU as / clang AArch64 语法红线

- 非 numeric-local label 不能以数字开头：用 `M4x4_loop:` 或 `.L4x4_M_loop:`，不要用 `4x4_M_loop:`；纯数字局部标签 `1:` / `1b` / `1f` 可以使用。
- `ld1` / `st1` post-index 立即数必须写成 `#imm`，且多寄存器列表的 `imm` 必须等于传输字节数：`ld1 {v0.4s, v1.4s}, [x0], #32`、`st1 {v0.4s-v3.4s}, [x2], #64`。
- `fmla` scalar-by-element 的第三个操作数必须是元素后缀，不是向量 arrangement：`fmla v16.4s, v0.4s, v1.s[0]`，不要写 `v1.4s[0]`。

## 宏定义模式

参考 dav1d/x264 的 `.macro` 模式，在 C 内联汇编中可用函数封装：

```c
// 4元素加法宏模板
static inline void add_4_elements_neon_asm(float *out, const float *a, const float *b) {
    __asm__ __volatile__(
        "ld1 {v0.4s}, [%[a]]\n"
        "ld1 {v1.4s}, [%[b]]\n"
        "fadd v0.4s, v0.4s, v1.4s\n"
        "st1 {v0.4s}, [%[out]]\n"
        : 
        : [out] "r" (out), [a] "r" (a), [b] "r" (b)
        : "memory", "v0", "v1"
    );
}

// 8元素加法宏模板
static inline void add_8_elements_neon_asm(float *out, const float *a, const float *b) {
    __asm__ __volatile__(
        "ld1 {v0.4s, v1.4s}, [%[a]]\n"
        "ld1 {v2.4s, v3.4s}, [%[b]]\n"
        "fadd v0.4s, v0.4s, v2.4s\n"
        "fadd v1.4s, v1.4s, v3.4s\n"
        "st1 {v0.4s, v1.4s}, [%[out]]\n"
        : 
        : [out] "r" (out), [a] "r" (a), [b] "r" (b)
        : "memory", "v0", "v1", "v2", "v3"
    );
}
```

## 循环展开模板

### 4路展开加法

```c
// 参考 ComputeLibrary pooling/generic.cpp 的展开模式
void add_array_unrolled4_asm(float *out, const float *a, const float *b, int n) {
    int i = 0;
    const int stride4 = 16;  // 4 * sizeof(float)
    
    // 预计算结束指针
    const float *end4 = a + (n & ~3);
    
    __asm__ __volatile__(
        "mov x0, %[a]\n"
        "mov x1, %[b]\n"
        "mov x2, %[out]\n"
        "mov x3, %[end4]\n"
        
        "1:\n"  // 主循环
        "cmp x0, x3\n"
        "b.ge 2f\n"
        
        // 加载 4 组数据（每组 4 float）
        "ld1 {v0.4s, v1.4s}, [x0], #32\n"
        "ld1 {v2.4s, v3.4s}, [x1], #32\n"
        "ld1 {v4.4s, v5.4s}, [x0], #32\n"
        "ld1 {v6.4s, v7.4s}, [x1], #32\n"
        
        // 计算
        "fadd v0.4s, v0.4s, v2.4s\n"
        "fadd v1.4s, v1.4s, v3.4s\n"
        "fadd v4.4s, v4.4s, v6.4s\n"
        "fadd v5.4s, v5.4s, v7.4s\n"
        
        // 存储 4 组结果
        "st1 {v0.4s, v1.4s}, [x2], #32\n"
        "st1 {v4.4s, v5.4s}, [x2], #32\n"
        
        "b 1b\n"
        
        "2:\n"  // 结束
        : 
        : [a] "r" (a), [b] "r" (b), [out] "r" (out), [end4] "r" (end4)
        : "memory", "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "x0", "x1", "x2", "x3"
    );
    
    // 尾处理（标量）
    for (i = n & ~3; i < n; i++) {
        out[i] = a[i] + b[i];
    }
}
```

### 延迟写入模式

参考 dav1d 的先计算后存储模式：

```c
// 先计算 4 行，再统一存储
void compute_then_store_asm(float *out, const float *a, const float *b, 
                             int rows, int cols) {
    // 每 4 行处理一次
    int r = 0;
    for (; r + 4 <= rows; r += 4) {
        float *out_rows[4];
        const float *a_rows[4], *b_rows[4];
        for (int i = 0; i < 4; i++) {
            out_rows[i] = out + (r + i) * cols;
            a_rows[i] = a + (r + i) * cols;
            b_rows[i] = b + (r + i) * cols;
        }
        
        __asm__ __volatile__(
            // 先加载所有数据
            "ld1 {v0.4s}, [%[a0]]\n"
            "ld1 {v1.4s}, [%[a1]]\n"
            "ld1 {v2.4s}, [%[a2]]\n"
            "ld1 {v3.4s}, [%[a3]]\n"
            "ld1 {v4.4s}, [%[b0]]\n"
            "ld1 {v5.4s}, [%[b1]]\n"
            "ld1 {v6.4s}, [%[b2]]\n"
            "ld1 {v7.4s}, [%[b3]]\n"
            
            // 计算 4 行
            "fadd v0.4s, v0.4s, v4.4s\n"
            "fadd v1.4s, v1.4s, v5.4s\n"
            "fadd v2.4s, v2.4s, v6.4s\n"
            "fadd v3.4s, v3.4s, v7.4s\n"
            
            // 延迟写入（计算完成后）
            "st1 {v0.4s}, [%[out0]]\n"
            "st1 {v1.4s}, [%[out1]]\n"
            "st1 {v2.4s}, [%[out2]]\n"
            "st1 {v3.4s}, [%[out3]]\n"
            :
            : [out0] "r" (out_rows[0]), [out1] "r" (out_rows[1]),
              [out2] "r" (out_rows[2]), [out3] "r" (out_rows[3]),
              [a0] "r" (a_rows[0]), [a1] "r" (a_rows[1]),
              [a2] "r" (a_rows[2]), [a3] "r" (a_rows[3]),
              [b0] "r" (b_rows[0]), [b1] "r" (b_rows[1]),
              [b2] "r" (b_rows[2]), [b3] "r" (b_rows[3])
            : "memory", "v0", "v1", "v2", "v3", "v4", "v5", "v6", "v7"
        );
    }
    
    // 尾处理
    for (; r < rows; r++) {
        for (int c = 0; c < cols; c++) {
            out[r*cols + c] = a[r*cols + c] + b[r*cols + c];
        }
    }
}
```

## 像素度量模板

### SAD (参考 x264 pixel.S)

```c
// 8x8 SAD 汇编模板
uint32_t sad_8x8_asm(const uint8_t *a, const uint8_t *b) {
    uint32_t result;
    __asm__ __volatile__(
        // 初始化累加器
        "movi v16.16b, #0\n"
        
        // 加载 8 行
        "ld1 {v0.8b}, [%[a]], #8\n"
        "ld1 {v1.8b}, [%[b]], #8\n"
        "ld1 {v2.8b}, [%[a]], #8\n"
        "ld1 {v3.8b}, [%[b]], #8\n"
        
        // 计算绝对差并累加
        "uabdl v17.8h, v0.8b, v1.8b\n"
        "uabdl v18.8h, v2.8b, v3.8b\n"
        "uadalp v16.4s, v17.8h\n"
        "uadalp v16.4s, v18.8h\n"
        
        // 继续处理剩余行...
        // (省略重复代码)
        
        // 最终水平归约
        "uaddlv s16, v16.4s\n"
        "mov %w[result], v16.s[0]\n"
        : [result] "=r" (result)
        : [a] "r" (a), [b] "r" (b)
        : "memory", "v0", "v1", "v2", "v3", "v16", "v17", "v18"
    );
    return result;
}
```

**关键指令**:
- `uabdl`: unsigned absolute difference long (扩展到 16-bit)
- `uadalp`: unsigned add accumulate long pairwise (水平累加)
- `uaddlv`: unsigned add long vector (最终水平归约)

### Dot Reduction 模板

```c
// 32元素 dot product
float dot_32_asm(const float *a, const float *b) {
    float result;
    __asm__ __volatile__(
        // 初始化累加器
        "movi v16.16b, #0\n"
        "movi v17.16b, #0\n"
        
        // 加载 8 组数据（每组 4 float）
        "ld1 {v0.4s}, [%[a]], #16\n"
        "ld1 {v1.4s}, [%[b]], #16\n"
        "ld1 {v2.4s}, [%[a]], #16\n"
        "ld1 {v3.4s}, [%[b]], #16\n"
        
        // 乘累加
        "fmla v16.4s, v0.4s, v1.4s\n"
        "fmla v17.4s, v2.4s, v3.4s\n"
        
        // 继续处理剩余数据...
        
        // 合并两个累加器
        "fadd v16.4s, v16.4s, v17.4s\n"
        
        // 水平归约
        "faddp s16, v16.4s\n"  // v16.s[0] + v16.s[1]
        "faddp s16, v16.2s\n"  // 完成归约
        
        "mov %[result], s16\n"
        : [result] "=r" (result)
        : [a] "r" (a), [b] "r" (b)
        : "memory", "v0", "v1", "v2", "v3", "v16", "v17"
    );
    return result;
}
```

## 尾处理模板

### Oddments 处理（参考 ComputeLibrary）

```c
// 尾处理汇编模板（1-3 元素）
void store_oddments_asm(float *out, float32x4_t v, int remaining) {
    __asm__ __volatile__(
        "cmp %w[n], #2\n"
        "b.lt 1f\n"
        
        // 存储 2 元素
        "st1 {v16.d}[0], [%[out]], #8\n"
        "sub %w[n], %w[n], #2\n"
        "cbz %w[n], 2f\n"
        
        "1:\n"  // 存储 1 元素
        "st1 {v16.s}[0], [%[out]], #4\n"
        
        "2:\n"  // 结束
        : 
        : [out] "r" (out), [n] "r" (remaining), [v] "r" (v)
        : "memory"
    );
}
```

## 寄存器分配规则

### 通用寄存器使用

- `x0-x7`: 输入参数
- `x8-x15`: 临时寄存器
- `x16-x17`: IP (Intra-Procedure-call scratch)
- `x19-x28`: callee-saved（需要保存）
- `x29`: FP (Frame Pointer)
- `x30`: LR (Link Register)

### 向量寄存器使用

- `v0-v7`: 参数传递 / 返回值
- `v8-v15`: callee-saved（低 64-bit）
- `v16-v31`: caller-saved（临时使用）

**建议**: 内联汇编中使用 `v16-v31` 作为临时寄存器，避免 callee-save 需求。

## clobber 列表规范

```c
__asm__ __volatile__(
    "..."
    : /* outputs */
    : /* inputs */
    : "memory",  // 内存可能被修改
      "v0", "v1", "v2", "v3",  // 使用了哪些向量寄存器
      "v16", "v17", "v18",
      "x0", "x1", "x2"  // 使用了哪些通用寄存器（除输入外）
);
```

**注意**:
- 输入参数寄存器不需要在 clobber 中列出
- `"memory"` 表示内存可能被修改
- `"cc"` 表示条件码被修改（可选）

## 相关文档

- `docs/width-dispatch-patterns.md`: 宽度跳转表模式
- `docs/sme-za-inline-asm-guide.md`: SME 汇编模板
- `docs/operator-patterns.md`: 算子模式识别
