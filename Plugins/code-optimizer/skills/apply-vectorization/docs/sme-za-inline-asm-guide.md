# SME ZA Inline ASM 生成指南

本文件是 `SME ZA/tile` standalone 代码生成的主参考。只要生成代码直接使用 ZA tile 或 ZA slice，并且目标链接环境没有预先验证 SME ABI support routines，就默认走这里的 inline asm 路径。

`codegen_style=auto` 下，如果输入是 C/C++ 且已有 `asm/__asm__`，进入 `inline_asm`；如果输入是 `.S/.s/.asm`，进入 `assembly` artifacts。两者都不能因为“已经是汇编”而直接拒绝，但必须完成 ZA 状态、ABI、clobber、符号和链接检查。

## 1. 官方来源

结构化记录写入 `references/arm_intrinsics_db/`，查询入口是：

```bash
python3 scripts/query_arm_intrinsics.py lookup --instruction FMOPA --isa sme --json
python3 scripts/query_arm_intrinsics.py lookup --instruction SMSTART --isa sme --json
python3 scripts/query_arm_intrinsics.py lookup --instruction ST1W --isa sme --json
python3 scripts/query_arm_intrinsics.py search --group inline-asm --isa sme --json
```

来源策略：

- [Arm C Language Extensions (ACLE)](https://arm-software.github.io/acle/main/acle.html): 用于稳定抓取 SME/SVE 指令分组、feature macro、函数属性和 intrinsics 入口。
- [Part 1: Arm Scalable Matrix Extension (SME) Introduction](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction): 用于核对 `SMSTART` / `SMSTOP` 和 streaming / ZA 状态说明。
- [Part 2: Arm Scalable Matrix Extension (SME) Instructions](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction-p2): 用于核对 `FMOPA`、ZA tile load/store/move、`ZERO {ZA}` 的语义和汇编形式。
- [DDI0602 SME Instructions](https://developer.arm.com/documentation/ddi0602/latest/SME-Instructions) 和 [DDI0602 SVE Instructions](https://developer.arm.com/documentation/ddi0602/latest/SVE-Instructions): 作为 authoritative instruction URL 保留。当前脚本环境访问这些页面可能返回 `403`，刷新脚本必须把它们标为 reference-only，而不是失败。

## 2. 指令表

| 角色 | 指令形态 | Inline asm 用法 |
| --- | --- | --- |
| ZA 状态开始 | `smstart za` | 在 `zero {za}`、`fmopa`、`mova`、ZA `st1w` 之前执行 |
| ZA 状态结束 | `smstop za` | 在最后一次 ZA 写回之后执行 |
| ZA 初始化 | `zero {za}` | fresh output tile 的 K 循环前执行一次 |
| 外积累加 | `fmopa za0.s, p0/m, p1/m, z0.s, z1.s` | FP32 outer-product 累加到 ZA tile |
| ZA 直接写回 | `st1w {za0h.s[w12, 0]}, p1, [xN]` | `beta == 0` 时把 ZA horizontal slice 写回 C |
| ZA 读出 | `mova z2.s, p1/m, za0h.s[w12, 0]` | `beta != 0` 时先把 ZA slice 读到 Z register |
| 谓词生成 | `whilelo p0.s, xzr, xN` | tile 边界、M/N 尾块和载入掩码 |
| 全真谓词 | `ptrue p1.s` | 只允许用于已证明完整的 tile 维度 |
| 谓词载入 | `ld1w { z0.s }, p0/z, [xN]` | 载入 A/B 向量，inactive lane 置零 |
| beta 路径 | `fadd` / `fmla` predicated vector forms | 把 ZA 结果和旧 C 合并 |

## 3. 函数边界

Standalone ZA inline asm 的默认函数边界是：

```c
#include <arm_sme.h>

void kernel(...) __arm_streaming;
void kernel(...) __arm_streaming {
    ...
}
```

规则：

- 生成源码默认不写 `__arm_new("za")`。
- 只有目标链接环境已经验证提供 SME ABI support routines 时，才允许单独选择 `__arm_new("za")` 路径。
- 不允许在生成源码里定义弱符号 `__arm_tpidr2_save`、`__arm_tpidr2_restore` 或 `__arm_za_disable`。
- inline asm clobber 至少包含 `"memory"` 和 `"za"`；实际使用到的 `p`、`z`、`w` 寄存器也必须列入 clobber。
- 如果 asm 修改条件码，额外加 `"cc"`。

Standalone `.S` artifacts 额外要求：

- wrapper 和 `.S` 暴露符号必须一致。
- 遵守 AAPCS64 参数寄存器、返回值和 callee-saved 寄存器规则。
- `.S` 中必须显式包含 `smstart za` / `smstop za` 状态范围，不能依赖 C 属性隐式获得 ZA。
- 汇编和最终链接产物都要扫描 `__arm_tpidr2_save`、`__arm_tpidr2_restore` 和 `__arm_za_disable`。

## 4. VL x VL SGEMM Tile 模板

输入 `A` 如果在原始布局中列向不连续，先在 C 外层把当前 K 对应的 A 行块连续化到 `a_work`，再进入 inline asm。不要在 `ld1w` 里伪造非连续访存。

```c
#include <arm_sme.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

static void sgemm_tile_za_inline(
    int row_count, int col_count, int k,
    const float *a_work, const float *b_panel,
    float *c_tile, int ldc, float beta) __arm_streaming;
static void sgemm_tile_za_inline(
    int row_count, int col_count, int k,
    const float *a_work, const float *b_panel,
    float *c_tile, int ldc, float beta) __arm_streaming {
    __asm__ volatile(
        "smstart za\n"
        "zero {za}\n"
        :
        :
        : "memory", "za"
    );

    for (int kk = 0; kk < k; ++kk) {
        const float *a_vec = a_work + (size_t)kk * row_count;
        const float *b_vec = b_panel + (size_t)kk * col_count;
        __asm__ volatile(
            "whilelo p0.s, xzr, %x[row_count]\n"
            "whilelo p1.s, xzr, %x[col_count]\n"
            "ld1w { z0.s }, p0/z, [%[a_vec]]\n"
            "ld1w { z1.s }, p1/z, [%[b_vec]]\n"
            "fmopa za0.s, p0/m, p1/m, z0.s, z1.s\n"
            :
            : [a_vec] "r"(a_vec),
              [b_vec] "r"(b_vec),
              [row_count] "r"((uint64_t)row_count),
              [col_count] "r"((uint64_t)col_count)
            : "memory", "p0", "p1", "z0", "z1", "za"
        );
    }

    uint32_t beta_bits;
    memcpy(&beta_bits, &beta, sizeof(beta_bits));
    for (int tile_row = 0; tile_row < row_count; ++tile_row) {
        float *c_row = c_tile + (size_t)tile_row * ldc;
        if (beta == 0.0f) {
            __asm__ volatile(
                "whilelo p1.s, xzr, %x[col_count]\n"
                "mov w12, %w[tile_row]\n"
                "st1w {za0h.s[w12, 0]}, p1, [%[c_row]]\n"
                :
                : [col_count] "r"((uint64_t)col_count),
                  [tile_row] "r"(tile_row),
                  [c_row] "r"(c_row)
                : "memory", "p1", "w12", "za"
            );
        } else {
            __asm__ volatile(
                "whilelo p1.s, xzr, %x[col_count]\n"
                "mov w12, %w[tile_row]\n"
                "mova z2.s, p1/m, za0h.s[w12, 0]\n"
                "ld1w { z3.s }, p1/z, [%[c_row]]\n"
                "dup z4.s, %w[beta_bits]\n"
                "fmla z2.s, p1/m, z3.s, z4.s\n"
                "st1w { z2.s }, p1, [%[c_row]]\n"
                :
                : [col_count] "r"((uint64_t)col_count),
                  [tile_row] "r"(tile_row),
                  [c_row] "r"(c_row),
                  [beta_bits] "r"(beta_bits)
                : "memory", "p1", "z2", "z3", "z4", "w12", "za"
            );
        }
    }

    __asm__ volatile(
        "smstop za\n"
        :
        :
        : "memory", "za"
    );
}
```

说明：

- `fmopa za0.s` 是 FP32 outer-product 累加核心。
- `st1w {za0h.s[w12, 0]}` 是具体可编译写法；文档或查询结果里的 `st1w {za0h.s[...]}` 只是占位说明。
- `mova` 在 clang 输出汇编里可能显示为 `mov z*.s, p*/m, za*h.s[...]`，扫描时同时看 ZA slice operand。
- `beta == 0` 直接 `st1w`；`beta != 0` 先 `mova` 读 ZA slice，再 `ld1w` 读旧 C，用 `fmla` 合并。

## 5. 禁止项

- 禁止把 `__arm_new("za")` 当作 standalone 默认实现。
- 禁止在源码中塞弱符号 stub 来“修好”链接。
- 禁止缺少 `smstart za` / `smstop za` 范围。
- 禁止缺少 `zero {za}` 就开始 fresh tile 累加。
- 禁止 `fmopa` 的 `p0` / `p1` 与 A/B/C 的 active 行列含义不一致。
- 禁止 edge tile 用 `ptrue` 覆盖部分列或部分行。
- 禁止 `success=true` 只附文字说明而没有编译、链接、汇编扫描和未解析符号扫描结果。

## 6. 最终验证

最低验证命令形态：

```bash
clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable -S candidate.c -o candidate.s
rg 'smstart[[:space:]]+za|zero[[:space:]]+\{za\}|fmopa|st1w|smstop[[:space:]]+za' candidate.s
rg '__arm_tpidr2|__arm_za_disable' candidate.s
clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable candidate.c driver.c -o candidate.compare
nm -u candidate.compare | rg '__arm_tpidr2|__arm_za_disable'
```

Standalone `.S` artifact 的最低验证命令形态：

```bash
clang -O3 -march=armv9.2-a+sme -c candidate_kernel.S -o candidate_kernel.o
clang -std=c11 -O3 -march=armv9.2-a+sme candidate_wrapper.c candidate_kernel.o driver.c -o candidate.compare
nm -u candidate.compare | rg '__arm_tpidr2|__arm_za_disable'
```

通过标准：

- 汇编里必须出现 `smstart za`、`zero {za}`、`fmopa`、`st1w`、`smstop za`。
- 汇编和链接产物中不得出现未验证的 `__arm_tpidr2_save`、`__arm_tpidr2_restore`、`__arm_za_disable` 依赖。
- 如果本地不能完成目标 SME 编译和链接，结果只能标成未验证草案或 `success=false`。
