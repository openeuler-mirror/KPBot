# SME ZA / Tile 决策指南

本文件只解决一个问题：什么时候 `apply-vectorization` 可以从 `SME streaming` 升级到 `ZA/tile`，什么时候必须停在 streaming-compatible 路径。

如果结论是“可以生成 standalone ZA/tile 代码”，下一步必须读取 `docs/sme-za-inline-asm-guide.md`。那里是 `smstart za`、`zero {za}`、`fmopa`、`mova`、`st1w`、`smstop za` 的模板、clobber 和验证命令主参考。

## 1. 先把三个概念拆开

### 1.1 Streaming SVE mode

这是运行模式问题。

你只需要回答：

- 当前函数是否运行在 streaming 模式下
- 当前代码是否只是在 SME 环境里执行 SVE 风格谓词循环

对应属性通常是：

- `__arm_streaming`
- `__arm_streaming_compatible`
- `__arm_locally_streaming`

### 1.2 ZA ownership

这是状态所有权问题。

你必须回答：

- `ZA` 是调用方传进来的，还是当前函数新建的
- 当前函数结束时 `ZA` 状态由谁继续持有

常见属性：

- `__arm_inout("za")`
- `__arm_new("za")`
- `__arm_out("za")`

### 1.3 Tile / tile-slice mapping

这是数据布局问题。

你必须回答：

- 哪个 tile 存哪块结果
- 行和列对应哪一维
- tile-slice 怎么推进
- 边界块什么时候 predication，什么时候写回

如果这一层讲不清楚，就说明当前还不应该生成 `ZA/tile` 代码。

## 2. 默认策略

在 `apply-vectorization` 里，默认策略是：

- 普通逐元素循环：停在 `__arm_streaming`
- masked 逐元素循环：停在 SVE 谓词或 SME streaming-compatible 路径
- sum/dot reduction：使用 NEON/SVE 或 SME streaming-compatible 向量累加器，不进入 ZA
- GEMV：默认不是 ZA/tile 入口，除非上游已经把它组织成明确的二维 tile 外积流水线
- blocked GEMM 但 `ZA` 语义不清楚：仍先停在 `__arm_streaming`
- 只有矩阵/outer-product/tile 语义都明确：才允许进入 `ZA/tile`

这不是保守，而是为了避免生成错误 ABI 的 SME 伪代码。

## 3. 什么时候只生成 streaming-compatible 代码

满足任一情况，就只生成 streaming-compatible 代码：

- `out[i] = a[i] + b[i]`
- `out[i] = a[i] * b[i]`
- `row[c] += bias[c]`
- `sum += x[i]`
- `dot += a[i] * b[i]`
- GEMV 行内 dot-product
- blocked GEMM 还停留在“向量寄存器逐列累加”阶段
- 当前只需要验证“在 SME 机器上能不能跑一条正确的 streaming 路径”

推荐模板：

```c
#include <arm_sme.h>
#include <stdint.h>

void kernel(...) __arm_streaming;
void kernel(...) {
    const int vl = (int)svcntw();
    for (int col = 0; col < n; col += vl) {
        svbool_t pg = svwhilelt_b32((uint64_t)col, (uint64_t)n);
        ...
    }
}
```

此时可以出现：

- `__arm_streaming`
- `svwhilelt_b32`
- `svld1_f32`
- `svdup_n_f32`
- `svmla_f32_x`
- `svst1_f32`

但不应该硬塞：

- `__arm_inout("za")`
- `__arm_new("za")`
- `svzero_za()`
- `mopa/mla` 或 `svmopa_*`

## 4. 什么时候才允许进入 ZA / tile

只有这些条件同时满足时才进入：

1. 循环本身已经是规则矩阵、rank-k、blocked GEMM 或 outer-product 语义
2. 结果更像“累加到二维块”，而不是“算完一条向量就写回”
3. 你能明确说出 tile / tile-slice 对应的矩阵维度
4. 你能解释 `K` 或 outer-product 累加维度如何推进
5. 你能解释 `ZA` 状态在调用边界上的所有权
6. 你能解释边界块的 predication 和最终写回

## 4.1 ZA Entry Checklist

生成 `ZA/tile` response 前，`safety_checks` 至少要覆盖：

- 输出 tile 对应哪块 `C` 或结果矩阵
- 行和列分别对应哪一维
- `K` / depth / outer-product 累加维度如何推进
- `ZA` 使用 `__arm_new("za")`、`__arm_inout("za")` 还是 `__arm_out("za")`
- 行尾、列尾和 K 尾部如何 predication
- ZA tile 或 tile-slice 在什么时候写回内存
- 最终对象或可执行文件是否还依赖 `__arm_tpidr2_save`、`__arm_tpidr2_restore` 或 `__arm_za_disable`

缺任一项时，不允许生成 ZA/tile 代码。

## 5. `ZA` 属性怎么选

### 5.0 先决定是否允许 SME ABI runtime 依赖

`__arm_new("za")` 是语义最直接的 fresh-ZA 属性，但 clang 会为 private-ZA lazy-save ABI 生成 `__arm_tpidr2_save` 等运行时依赖。对 standalone benchmark、单文件生成物、未知远端环境，不能假设这些符号存在。

硬规则：

- 只有在目标链接环境已经验证提供 SME ABI support routines 时，才允许输出依赖 `__arm_new("za")` 的代码
- 不允许在生成源码里塞弱符号 stub 来“修好”链接；这会隐藏真实 ABI 约束
- 如果用户要求“生成出来一把能运行”且目标环境不保证 SME ABI runtime，ZA 路径应改为显式 inline asm：函数保持 `__arm_streaming`，内部手动 `smstart za`、`zero {za}`、`fmopa`、`st1w`、`smstop za`
- inline asm 的具体写法必须来自 `docs/sme-za-inline-asm-guide.md` 或 `query_arm_intrinsics.py search --group inline-asm --isa sme --json`
- 生成 inline asm 路径后必须编译到汇编并扫描，确认没有 `bl __arm_tpidr2_save` 或同类 SME ABI runtime 调用

最小验证命令形态：

```bash
clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable -S candidate.c -o candidate.s
rg '__arm_tpidr2|__arm_za_disable|fmopa|smstart|smstop|st1w' candidate.s
clang -std=c11 -O3 -march=armv9.2-a+sme -msve-vector-bits=scalable candidate.c driver.c -o candidate.compare
nm -u candidate.compare | rg '__arm_tpidr2|__arm_za_disable'
```

最后一条必须没有输出，除非目标平台真实 runtime 已确认提供这些符号。

### 5.1 `__arm_inout("za")`

适合：

- 调用方和被调方共享同一份 `ZA`
- 当前函数只是一个更大 tile 累加流水线中的片段

风险：

- 这是 ABI 契约，不是普通注解
- 上下游函数签名和调用约定必须一起成立

### 5.2 `__arm_new("za")`

适合：

- 当前函数自己创建新的 `ZA`
- 累加状态从这里开始构建

通常意味着：

- 你很可能还需要 `svzero_za()`
- 你要解释为什么 fresh-ZA 生命周期只在当前函数内成立
- 你必须证明链接环境提供 SME ABI runtime，或者证明最终汇编和可执行文件没有 `__arm_tpidr2_*` 依赖

### 5.3 `__arm_out("za")`

适合：

- 当前函数新建并输出一份新的 `ZA` 状态给调用方继续消费

这比 `__arm_new("za")` 更强调“结果仍保留在 ZA 里向后传递”。

## 6. 什么时候需要 `svzero_za()`

当满足这三条时，优先考虑：

- 当前函数走 fresh-ZA 路径
- 结果需要从零开始累加
- 后续要用 outer-product / `mopa` / `mla` 一类矩阵操作

如果当前只是普通向量循环，通常不需要它。

## 7. `mopa/mla` 适用场景

`mopa/mla` 或 `svmopa_*` 这类矩阵操作更适合：

- outer-product 风格累加
- tile 级 blocked GEMM
- 结果在 `ZA` 中停留多个阶段后再统一写回

不适合：

- 只做一条向量 FMA 后立刻写回
- 没有 tile 结构的普通逐元素循环

## 8. tile / tile-slice 至少要说明到什么程度

一旦生成 `ZA/tile` 方案，输出里至少要解释：

- 哪个 tile 或 tile 组对应哪块输出
- 行和列分别是哪一维
- tile-slice 如何推进
- 边界块如何 predication
- 写回发生在什么时候
- 入口/退出如何管理 streaming mode 和 ZA 状态
- 编译、链接、未解析符号扫描的验证结果

如果解释不到这一步，就退回 streaming-compatible 路径。

## 9. 典型拒绝语句

这些拒绝说明是合理的：

- “当前循环只有向量级累加，还没有清晰的 ZA ownership，因此先停在 `__arm_streaming` 路径。”
- “虽然是 blocked GEMM，但没有定义 tile-slice 写回和调用边界，不能安全生成 `__arm_inout(\"za\")` 方案。”
- “当前场景存在间接寻址或输出冲突，既不能做 streaming 向量化，也不能做 ZA/tile 重写。”
- “当前循环是 sum/dot reduction，不具备二维 tile 累加语义，因此不能进入 `ZA/tile`。”
