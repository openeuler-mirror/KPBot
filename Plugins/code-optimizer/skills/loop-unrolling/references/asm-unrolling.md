# 汇编路径循环展开

本文件为 `loop-unrolling` Skill 的汇编展开路径参考，处理 `pure_asm` 和 `inline_asm` 两种语言类型。

## 步骤 1A：分析汇编循环结构

1. 用 Read 工具读取 `source_file` 中 `lines[0]` 到 `lines[1]` 的汇编代码

2. **自动检测代码类型**：
   - 检测 NEON / SVE 寄存器和指令 → 确定 simd_type
   - 无 SIMD 指令 → `scalar`

3. **识别循环元素**（逐项定位，展开时需精确调整）：
   - **循环入口标签**：如 `.Lloop:`、`.L2:`，展开后该标签不变
   - **循环计数器**：倒计数用 `subs` 减数的寄存器；正计数用 `add` 递增的寄存器
   - **循环条件**：`b.ne`/`b.lt`/`b.hi` 等分支指令，展开后步进需调整（→ 3A-4）
   - **循环体**：标签与分支之间的指令序列，展开时复制 N 份
   - **循环增量**：`add`/`incw`/后索引 `,#imm` 等，展开后步进乘 N

4. **判断循环体复杂度**：
   - **可展开**：循环体仅含算术/访存指令，无条件分支和函数调用
   - **不可展开**：循环体含条件分支（`b.eq`/`cbz`/`tbz` 等非循环控制分支）或 `bl` 函数调用 → 返回 `success=false`

5. **分析数据访问模式**（决定偏移量调整策略 → 3A-3）：
   - **后索引寻址**：`ldr q0, [x0], #16` — 基址自动递增，展开时保持后索引不变
   - **固定偏移寻址**：`ldr q0, [x1, x3, lsl #2]` — 用偏移寄存器索引，展开时递增偏移量
   - **预索引寻址**：`ldr q0, [x0, #16]!` — 类似后索引但先递增再访问
   - 同一循环可能混合多种模式，按主导模式处理

6. **分析并行度**：统计循环体内独立的操作数量，记为 `current_parallelism`

7. **分析数据依赖**：
   - **累加型依赖**（如 `fmla v0, v1, v2` 中 v0 既是源又是目标）→ 可拆分累加器（→ 3A-2）
   - **地址依赖**（如 `ldr x0, [x0]` 用于下一轮基址）→ 通常可展开，但需保留基址递增
   - **循环携带依赖**（迭代 i 的计算结果被 i+1 使用，如 `fmadd s0, s1, s2, s0` 后 s0 作为下轮输入但非累加）→ **不可展开**，返回 `success=false`

8. **识别寄存器使用**：统计通用和向量寄存器使用，区分可重命名（临时）和不可重命名（累加器、基址、计数器）

## 步骤 3A：生成汇编展开代码

### 3A-1. 寄存器重命名

展开后每份循环体使用独立的临时寄存器。按可用性优先级分配：

**两档寄存器评估**：

| 档位 | 通用寄存器 | NEON/SVE 寄存器 | 条件 |
|------|-----------|----------------|------|
| 默认档 | `x8-x17`, `x30`（11 个） | `v0/z0`-`v7/z7`, `v16/z16`-`v31/z31`（24 个） | 无栈帧保护 |
| 扩展档 | +`x19-x29`（11 个） | +`v8/z8`-`v15/z15`（8 个） | 需栈帧保护 |

- 默认档不足时自动升级到扩展档；扩展档仍不足时降低 `unroll_factor`
- **SIMD callee-saved 只保存低 64 位**（`d8-d15`），AAPCS64 不要求保存高 64 位
- `x0-x7` 为参数/返回值，`x18` 平台保留，不可使用

**栈帧保护**：当需要 callee-saved 寄存器时，在函数入口 `stp` 保存、退出前 `ldp` 恢复。仅保存实际使用的寄存器。

**不可重命名**：累加器（需拆分）、基址寄存器（偏移量区分）、循环计数器（调整步进）、谓词寄存器

### 3A-2. 累加器拆分

如果展开后复用同一累加寄存器（如 `fmla v0` 连续 4 次），会形成串行依赖链：每次 fmla 必须等上一次完成才能发射。拆分为独立累加器可打断依赖链，让多条 fmla 并行执行。

归约类操作将单个累加器拆分为 `unroll_factor` 个独立累加器，循环结束后合并：

```assembly
// 展开前：单个 NEON 累加器（串行依赖链）
fmla v0.4s, v1.4s, v2.4s    // v0 = v0 + v1*v2

// 展开后（unroll_factor=4）：4 个独立累加器
fmla v0.4s, v1.4s, v2.4s    // sum0 += a[i]*b[i]
fmla v3.4s, v4.4s, v5.4s    // sum1 += a[i+4]*b[i+4]
fmla v6.4s, v7.4s, v8.4s    // sum2 += a[i+8]*b[i+8]
fmla v9.4s, v10.4s, v11.4s  // sum3 += a[i+12]*b[i+12]

// 循环结束后合并
fadd v0.4s, v0.4s, v3.4s
fadd v6.4s, v6.4s, v9.4s
fadd v0.4s, v0.4s, v6.4s
```

SVE 和标量路径同理：拆分累加器后 fadd 合并。

### 3A-3. 偏移量调整

**后索引寻址** — 保持后索引不变，每份展开体用独立寄存器：

```assembly
// 展开前（后索引循环）
.Lloop:
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
    subs x2, x2, #1
    b.ne .Lloop

// 展开后（unroll_factor=2，独立寄存器 + 合并累加器）
.Lloop:
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
    ldr q3, [x0], #16
    fmla v4.4s, v3.4s, v1.4s
    subs x2, x2, #2
    b.ne .Lloop

// 循环结束后合并累加器
fadd v2.4s, v2.4s, v4.4s
```

**固定偏移寻址** — 每份展开体递增偏移量：

```assembly
// 展开前
.Lloop:
    ldr q0, [x1, x3, lsl #2]
    ldr q5, [x2, x3, lsl #2]
    fmla v0.4s, v5.4s, v6.4s
    st1 {v0.4s}, [x0, x3, lsl #2]
    add x3, x3, #4
    cmp x3, x4
    b.lt .Lloop

// 展开后（unroll_factor=2，偏移递增 + 独立寄存器）
.Lloop:
    ldr q0, [x1, x3, lsl #2]
    ldr q5, [x2, x3, lsl #2]
    fmla v0.4s, v5.4s, v6.4s
    st1 {v0.4s}, [x0, x3, lsl #2]

    ldr q7, [x1, x3, lsl #2]     // 第二份
    ldr q8, [x2, x3, lsl #2]
    fmla v7.4s, v8.4s, v6.4s
    st1 {v7.4s}, [x0, x3, lsl #2]

    add x3, x3, #8                // 步进 2×4 = 8
    cmp x3, x4
    b.lt .Lloop
```

- NEON float32 每份步进 4 元素 = 16 字节；float64 每份步进 2 元素 = 16 字节
- SVE 每份步进 VL 字节（由 `incw`/`incd` 控制）

### 3A-4. 调整循环控制

**主循环迭代次数预计算**：进入展开循环前，需计算主循环迭代次数（总迭代 / unroll_factor），若不足一轮则跳过主循环直接走尾处理：

```assembly
// 倒计数模式：原循环 subs x2, x2, #1
// 展开后 subs x2, x2, #2 → 需保证 x2 >= 2 才进入主循环
subs x2, x2, #2          // 预减一轮，同时设置标志
b.lt .Lepilogue          // 不足一轮则跳到尾处理
.Lloop:
    ...展开后的循环体...
    subs x2, x2, #2
    b.ge .Lloop
.Lepilogue:
    tbz x2, #0, .Ldone   // 处理余数
    ...
```

**步进调整**：
- 倒计数：`subs Xn, Xn, #imm` → `subs Xn, Xn, #(imm × unroll_factor)`
- 正计数：步进乘 `unroll_factor`

### 3A-5. 尾处理

如果循环迭代次数不能被 `unroll_factor` 整除，添加尾处理代码：

```assembly
    // 主循环（展开 2x）
.Lloop:
    ...展开后的循环体...
    subs x2, x2, #2
    b.ne .Lloop

    // 尾处理（处理剩余迭代）
.Lepilogue:
    tbz x2, #0, .Ldone       // x2 为 0 则跳过
    ldr q0, [x0], #16
    fmla v2.4s, v0.4s, v1.4s
.Ldone:

    // 合并累加器
    fadd v2.4s, v2.4s, v4.4s
```

**SVE 尾处理**：`whilelt` 谓词自动处理，无需独立尾循环：

```assembly
.Lloop:
    whilelt p0.s, x3, x4
    ld1w {z0.s}, p0/z, [x1, x3, lsl #2]
    ld1w {z1.s}, p0/z, [x2, x3, lsl #2]
    fmla z0.s, p0/m, z1.s, z2.s

    add x3, x3, x5            // x5 = svcntw()
    whilelt p0.s, x3, x4
    ld1w {z3.s}, p0/z, [x1, x3, lsl #2]
    ld1w {z4.s}, p0/z, [x2, x3, lsl #2]
    fmla z3.s, p0/m, z4.s, z2.s

    add x3, x3, x5
    cmp x3, x4
    b.lt .Lloop

    // 合并 SVE 累加器
    fadd z0.s, z0.s, z3.s
```

### 3A-6. 内联汇编特殊处理

当 `language == "inline_asm"` 时：
1. 仅修改 asm 字符串内容，不修改外围 C 代码
2. `"=r"`/`"+r"` 输出操作数对应的 C 变量不需要在 asm 中重命名
3. **展开后必须更新 clobber 列表**，包含所有新使用的寄存器
4. 输入操作数在每份展开体中可复用

```c
// 展开前
asm volatile(
    "ldr q0, [%[a]]\n\t"
    "ldr q1, [%[b]]\n\t"
    "fmla v2.4s, v0.4s, v1.4s\n\t"
    : [out] "=w" (result)
    : [a] "r" (a_ptr), [b] "r" (b_ptr)
    : "v0", "v1", "memory"
);

// 展开后（unroll_factor=2）— 注意 clobber 列表更新
asm volatile(
    "ldr q0, [%[a]]\n\t"
    "ldr q1, [%[b]]\n\t"
    "fmla v2.4s, v0.4s, v1.4s\n\t"
    "ldr q3, [%[a], #16]\n\t"
    "ldr q4, [%[b], #16]\n\t"
    "fmla v5.4s, v3.4s, v4.4s\n\t"
    : [out] "=w" (result)
    : [a] "r" (a_ptr), [b] "r" (b_ptr)
    : "v0", "v1", "v3", "v4", "v5", "memory"
);
```

## 完整展开示例

### 示例1：标量 ARM64 点积展开（unroll_factor=4）

```assembly
// 展开前：标量点积，正计数循环
//   x0 = a*, x1 = b*, x2 = n
dot_product:
    mov  w3, #0
    fmov s0, #0.0
.Lloop:
    cmp  w3, w2
    b.ge .Lend
    ldr  s1, [x0, w3, sxtw #2]
    ldr  s2, [x1, w3, sxtw #2]
    fmadd s0, s1, s2, s0
    add  w3, w3, #1
    b    .Lloop
.Lend:
    ret

// 展开后：4 个独立标量累加器 + 尾处理
// 用后索引寻址避免每份之间手动递增索引
dot_product_unrolled:
    fmov s0, #0.0                // sum0
    fmov s1, #0.0                // sum1
    fmov s2, #0.0                // sum2
    fmov s3, #0.0                // sum3

    // 主循环 guard：n < 4 则跳过
    cmp  w2, #4
    b.lt .Ltail

    // 保存原始指针，用后索引递增
    mov  x3, x0
    mov  x4, x1

.Lloop:
    ldr   s4, [x3], #4                // a[i], x3 += 4
    ldr   s5, [x4], #4                // b[i], x4 += 4
    fmadd s0, s4, s5, s0              // sum0

    ldr   s6, [x3], #4                // a[i+1]
    ldr   s7, [x4], #4                // b[i+1]
    fmadd s1, s6, s7, s1              // sum1

    ldr   s16, [x3], #4               // a[i+2]
    ldr   s17, [x4], #4               // b[i+2]
    fmadd s2, s16, s17, s2            // sum2

    ldr   s18, [x3], #4               // a[i+3]
    ldr   s19, [x4], #4               // b[i+3]
    fmadd s3, s18, s19, s3            // sum3

    subs  w2, w2, #4                  // n -= 4
    b.ne  .Lloop

    // 合并累加器
    fadd s0, s0, s1
    fadd s2, s2, s3
    fadd s0, s0, s2

.Ltail:
    // 标量尾处理（剩余 0-3 个）
    cbz  w2, .Lend
.Ltail_loop:
    ldr   s4, [x3], #4
    ldr   s5, [x4], #4
    fmadd s0, s4, s5, s0
    subs  w2, w2, #1
    b.ne  .Ltail_loop
.Lend:
    ret
```

### 示例2：NEON 点积展开（unroll_factor=4，后索引寻址）

```assembly
// 展开前：NEON 向量点积，后索引循环
//   x0 = a*, x1 = b*, x2 = n（已对齐到 4 的倍数）
dot_product_neon:
    movi  v0.4s, #0                   // sum_vec = 0
.Lloop:
    ld1   {v1.4s}, [x0], #16          // 加载 a[i..i+3]，x0 += 16
    ld1   {v2.4s}, [x1], #16          // 加载 b[i..i+3]，x1 += 16
    fmla  v0.4s, v1.4s, v2.4s        // sum_vec += a * b
    subs  x2, x2, #4                  // n -= 4
    b.ne  .Lloop

    // 水平求和
    faddp v0.4s, v0.4s, v0.4s
    faddp v0.4s, v0.4s, v0.4s
    ret

// 展开后：4 个独立 NEON 累加器，打断 fmla 串行依赖链
dot_product_neon_unrolled:
    movi  v0.4s, #0                   // sum0
    movi  v1.4s, #0                   // sum1
    movi  v2.4s, #0                   // sum2
    movi  v3.4s, #0                   // sum3

    // 主循环 guard
    subs  x2, x2, #16                 // 预减 4×4=16，同时设标志
    b.lt  .Lepilogue

.Lloop:
    ld1   {v4.4s}, [x0], #16          // a[i..i+3]
    ld1   {v5.4s}, [x1], #16
    fmla  v0.4s, v4.4s, v5.4s        // sum0

    ld1   {v6.4s}, [x0], #16          // a[i+4..i+7]
    ld1   {v7.4s}, [x1], #16
    fmla  v1.4s, v6.4s, v7.4s        // sum1

    ld1   {v16.4s}, [x0], #16         // a[i+8..i+11]
    ld1   {v17.4s}, [x1], #16
    fmla  v2.4s, v16.4s, v17.4s      // sum2

    ld1   {v18.4s}, [x0], #16         // a[i+12..i+15]
    ld1   {v19.4s}, [x1], #16
    fmla  v3.4s, v18.4s, v19.4s      // sum3

    subs  x2, x2, #16
    b.ge  .Lloop

.Lepilogue:
    // 处理余数（0-15 个 float）
    tbz   x2, #3, .Lcheck4           // 测试剩余是否 >= 8
    ld1   {v4.4s}, [x0], #16
    ld1   {v5.4s}, [x1], #16
    fmla  v0.4s, v4.4s, v5.4s
    ld1   {v6.4s}, [x0], #16
    ld1   {v7.4s}, [x1], #16
    fmla  v1.4s, v6.4s, v7.4s
    sub   x2, x2, #8
.Lcheck4:
    tbz   x2, #2, .Lcheck_remainder  // 测试剩余是否 >= 4
    ld1   {v4.4s}, [x0], #16
    ld1   {v5.4s}, [x1], #16
    fmla  v2.4s, v4.4s, v5.4s
    sub   x2, x2, #4
.Lcheck_remainder:
    // 标量余数（0-3 个 float）
    cbz   x2, .Lmerge
.Lscalar_tail:
    ldr   s4, [x0], #4
    ldr   s5, [x1], #4
    fmadd s3, s4, s5, s3              // 复用 v3 低 lane 做标量累加
    subs  x2, x2, #1
    b.ne  .Lscalar_tail

.Lmerge:
    // 合并 4 个累加器
    fadd  v0.4s, v0.4s, v1.4s
    fadd  v2.4s, v2.4s, v3.4s
    fadd  v0.4s, v0.4s, v2.4s

    // 水平求和
    faddp v0.4s, v0.4s, v0.4s
    faddp v0.4s, v0.4s, v0.4s
    ret
```

### 示例3：SVE 点积展开（unroll_factor=2，谓词尾处理）

```assembly
// 展开前：SVE 向量点积
//   x0 = a*, x1 = b*, x2 = n
dot_product_sve:
    ptrue p0.s
    fmov  z0.s, #0.0                  // sum_vec = 0
    mov   x3, #0                      // i = 0
    cntw  x4                          // x4 = VL/4 (floats per vector)
.Lloop:
    whilelt p0.s, x3, x2
    ld1w  {z1.s}, p0/z, [x0, x3, lsl #2]
    ld1w  {z2.s}, p0/z, [x1, x3, lsl #2]
    fmla  z0.s, p0/m, z1.s, z2.s
    add   x3, x3, x4
    whilelt p0.s, x3, x2
    b.none .Lend                      // 无活跃元素则退出
    b     .Lloop
.Lend:
    // 水平求和
    faddv s0, p0, z0.s
    ret

// 展开后：2 个独立 SVE 累加器，whilelt 自然处理尾部
dot_product_sve_unrolled:
    ptrue p0.s
    fmov  z0.s, #0.0                  // sum0
    fmov  z1.s, #0.0                  // sum1
    mov   x3, #0
    cntw  x5                          // x5 = VL/4

.Lloop:
    whilelt p0.s, x3, x2             // 第一份谓词
    ld1w  {z2.s}, p0/z, [x0, x3, lsl #2]
    ld1w  {z3.s}, p0/z, [x1, x3, lsl #2]
    fmla  z0.s, p0/m, z2.s, z3.s     // sum0

    add   x3, x3, x5                 // i += VL
    whilelt p0.s, x3, x2             // 第二份谓词
    ld1w  {z4.s}, p0/z, [x0, x3, lsl #2]
    ld1w  {z5.s}, p0/z, [x1, x3, lsl #2]
    fmla  z1.s, p0/m, z4.s, z5.s     // sum1

    add   x3, x3, x5
    cmp   x3, x2
    b.lt  .Lloop

    // 合并累加器
    fadd  z0.s, z0.s, z1.s

    // 水平求和
    faddv s0, p0, z0.s
    ret
```

### 示例4：NEON 向量加法展开（unroll_factor=4，后索引寻址 + 存储）

```assembly
// 展开前：a[i] = b[i] + c[i]，后索引循环
//   x0 = dst*, x1 = src_b*, x2 = src_c*, x3 = n
vector_add_neon:
    movi  v2.4s, #0
.Lloop:
    ld1   {v0.4s}, [x1], #16          // b[i..i+3], x1 += 16
    ld1   {v1.4s}, [x2], #16          // c[i..i+3], x2 += 16
    add   v0.4s, v0.4s, v1.4s
    st1   {v0.4s}, [x0], #16          // dst[i..i+3], x0 += 16
    subs  x3, x3, #4
    b.ne  .Lloop
    ret

// 展开后：4 份独立寄存器，后索引自动递增基址
vector_add_neon_unrolled:
    // 主循环 guard
    subs  x3, x3, #16                 // 预减 4×4=16
    b.lt  .Lvec_tail

.Lloop:
    ld1   {v0.4s}, [x1], #16          // b[i..i+3]
    ld1   {v1.4s}, [x2], #16
    add   v0.4s, v0.4s, v1.4s
    st1   {v0.4s}, [x0], #16

    ld1   {v2.4s}, [x1], #16          // b[i+4..i+7]
    ld1   {v3.4s}, [x2], #16
    add   v2.4s, v2.4s, v3.4s
    st1   {v2.4s}, [x0], #16

    ld1   {v16.4s}, [x1], #16         // b[i+8..i+11]
    ld1   {v17.4s}, [x2], #16
    add   v16.4s, v16.4s, v17.4s
    st1   {v16.4s}, [x0], #16

    ld1   {v18.4s}, [x1], #16         // b[i+12..i+15]
    ld1   {v19.4s}, [x2], #16
    add   v18.4s, v18.4s, v19.4s
    st1   {v18.4s}, [x0], #16

    subs  x3, x3, #16
    b.ge  .Lloop

.Lvec_tail:
    // 向量余数（4-15 个 float）
    tbz   x3, #3, .Lcheck4            // 剩余 >= 8?
    ld1   {v0.4s}, [x1], #16
    ld1   {v1.4s}, [x2], #16
    add   v0.4s, v0.4s, v1.4s
    st1   {v0.4s}, [x0], #16
    ld1   {v0.4s}, [x1], #16
    ld1   {v1.4s}, [x2], #16
    add   v0.4s, v0.4s, v1.4s
    st1   {v0.4s}, [x0], #16
    sub   x3, x3, #8
.Lcheck4:
    tbz   x3, #2, .Lscalar_tail       // 剩余 >= 4?
    ld1   {v0.4s}, [x1], #16
    ld1   {v1.4s}, [x2], #16
    add   v0.4s, v0.4s, v1.4s
    st1   {v0.4s}, [x0], #16

.Lscalar_tail:
    // 标量余数（0-3 个 float）
    ands  x3, x3, #3
    b.eq  .Lend
.Lscalar_loop:
    ldr  s0, [x1], #4
    ldr  s1, [x2], #4
    add  s0, s0, s1
    str  s0, [x0], #4
    subs x3, x3, #1
    b.ne .Lscalar_loop
.Lend:
    ret
```
