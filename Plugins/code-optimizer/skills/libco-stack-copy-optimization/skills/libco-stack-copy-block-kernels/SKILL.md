---
name: libco-stack-copy-block-kernels
description: >
  用于 libco 共享栈的 NEON 块拷贝；按尺寸选择块大小并保证块完整。
  适用于 libco 共享栈、AArch64 NEON 栈拷贝、需要减少循环次数/分支的块拷贝。
  触发词：libco 栈拷贝、NEON 块、块大小、块完整性、寄存器复用。
  不适用：非 AArch64 平台。
---

# NEON 分块内核

## 适用条件

- 共享栈切换的拷贝长度通常 ≥128B，主成本在块循环与分支。
- AArch64 NEON 可用；非 AArch64 使用 libc `memcpy`。
- 需要控制 q8-q15：它们是 callee-saved SIMD，内联汇编不得随意使用。

## 方法

### 1. 块大小

参考实现保留两个块内核：
- 128B：8×16B（q0-q7），用于 <=1KB。
- 384B：12×32B（q0-q7/q16-q31 分组），用于 >1KB。

256B/512B/1024B/1536B 属于历史实验，不作为默认档位。本 skill 按 1KB 阈值分档；tier 调优是可选扩展，不在本 skill 范围内。
384B 块拷完后若剩余 `>=128B`，先消费完整 128B 子块，
只有剩余 `<128B` 才进入余数/尾路径。例如 1280B = 3×384B + 128B，必须再走一个
128B 块。

### 2. 参数化 ld/st

```c
#define memcpy_block_save(dst, src, len, rem) \
    memcpy_block_neon(dst, src, len, rem, "ldp", "stnp")
#define memcpy_block_swap(dst, src, len, rem) \
    memcpy_block_neon(dst, src, len, rem, "ldnp", "stp")
```

同一套内核用宏参数切换 temporal hint，避免维护两份代码。

### 3. 128B 块骨架

```asm
ldp q0, q1, [src]
ldp q2, q3, [src, #32]
ldp q4, q5, [src, #64]
ldp q6, q7, [src, #96]
stp q0, q1, [dst]       /* save 用 stnp */
stp q2, q3, [dst, #32]
stp q4, q5, [dst, #64]
stp q6, q7, [dst, #96]
```

大块按 128B 子块组合；load/store 交错，寄存器分组复用。

### 4. 不要用 q8-q15

q8-q15 是 AAPCS64 callee-saved SIMD。内联汇编如果使用它们，会污染调用者的
double/64-bit SIMD 状态；除非外层显式保存，否则只用 q0-q7、q16-q31。
本方法集的块内核遵循这一约束。

### 5. 块必须完整

每个块循环必须覆盖整个块的所有字节。不要把 512B 块写成 384B 后仍按 512B 前进；
这是历史实现的真实缺陷（见验证矩阵）。

## 验证

- 每个块大小单独测：长度 = 块大小 × N + 余数；逐字节比对。
- tier 临界：1KB/384B 边界（383/384/385、1023/1024/1025），以及
  2047/2048/2049、4095/4096、8191/8192、12287/12288/12289。
- 前后哨兵；检查越界写。
- 用 `objdump` 确认 q8-q15 未出现在内核 clobber 列表；确认块内偏移覆盖完整。
- 验收必须回到协程切换；direct copy 只解释拷贝路径，不代表业务收益。

## 反例

- 不要把所有 >=128B 统一成 384B：该实验曾被回滚。
- 不要为了减循环次数无限加深展开；寄存器压力/I-cache 压力会上升，是否加深由 PMU/反汇编决定。
- 不要使用 q8-q15 而不保存；也不要指望调用者替你 spill。
- 如果改用了 d8-d15，普通函数返回前必须恢复并在内联汇编 clobber 中声明；否则调用方
  在内核返回时就会读到错误值。
- 不要只测首尾字节；块内漏拷可能只在特定偏移暴露。

## 迁移到其他应用

- 把块大小当假设而非常量：先统计目标应用的尺寸分布，再决定 128B/384B 起点。
- 若目标不是 AArch64/NEON，跳过本方法，保留 libc 或标量实现。
- 块内核必须覆盖完整块；用逐字节矩阵和 tier 临界 ±1 验证，不要只测吞吐。

## 证据

- 块内核按寄存器分组与完整块覆盖设计；参考实现保留 128B/384B 两档。
- 本 skill 固化方法与门禁，不固化性能数字；目标应用需按自身负载复测。
- 共享参考：包级 `references/libco-baseline-and-method-map.md`、`references/other-applicability.md`。
