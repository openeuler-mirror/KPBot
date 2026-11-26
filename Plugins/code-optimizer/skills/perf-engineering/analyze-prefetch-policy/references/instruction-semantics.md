# 预取指令判读

只解释观测到的指令/参数，不把名称翻译成保证。具体映射以相同工具链、目标 CPU 和最终产物为准。

| 观测 | 可记录的意图 | 不能据此断言 |
|---|---|---|
| AArch64 `prfm pldl1keep` | 数据读、L1、时间性保留提示 | 已命中 L1、一定从内存取来 |
| AArch64 `prfm pldl2keep` | 数据读、L2、时间性保留提示 | 保证绕开 L1、执行后已驻留 L2 |
| `pstl1keep` / `pstl2keep` | 数据写预取提示 | 这是普通读取预取，或已经写入数据 |
| `pldl1strm` / `pldl2strm` | 数据读、streaming 提示 | 使用后立即驱逐、一定不缓存 |
| `pli...` | 指令取指预取 | L1D/L2D 数据预取 |
| SVE `prfb/prfh/prfw/prfd` | 按具体寻址形式与谓词控制的预取，层级/读写意图取自 hint 操作数 | 仅根据 b/h/w/d 后缀判层级，或假定全部 lane 生效 |
| x86 `_MM_HINT_T0` / `prefetcht0` | T0 时间局部性提示，通常用于靠近消费时预取 | 在所有微架构上固定填充方式相同 |
| x86 `_MM_HINT_T1` / `prefetcht1` | T1 时间局部性提示，常用于较远的 L2 预取意图 | 与 Arm L2 提示在性能/填充行为上完全等价 |
| x86 NTA、T2、写预取扩展 | 原样记录 hint；按对应处理器手册解释 | 凭名称归入 L1/L2 读取策略 |
| `__builtin_prefetch(addr, rw, locality)` | rw 和 locality 是语义参数，需查看对应编译器版本 | locality=2 就是 L2；仅调用即保证生成指令 |
| ACLE `__pldx(access_kind, cache_level, retention_policy, addr)` | 三个独立常量参数分别表示读写、目标层级和保留提示；地址在最后 | 套用 GCC builtin 的参数顺序，或把层级编码直接当 L 编号 |

对 C/C++ builtin，常见 `rw=0` 为读、`rw=1` 为写；新版本可能扩展参数取值，查实际版本文档。locality 0–3 表示不同时间局部性，不是缓存层级编号。Rust 或其他语言封装也要展开到 intrinsic 或 asm。

即使 ISA 把预取视作非故障提示，也需单独检查**地址表达式的求值**：`p->next` 先解引用 p，Rust `ptr.add()` 还有语言层面的前置条件。不得由“预取不读取数据”推出任意指针运算都安全。本技能报告现有代码的前置条件，不运行无效地址实验。

Arm 数字 hint（如 `prfm #2, [...]`）必须根据准确编码解码，不把数字直接当缓存层级。编译器可能消除、移动或插入预取；以最终符号和调用路径为证。

## ACLE 参数判读

在 `<arm_acle.h>` 的 `__pldx(access_kind, cache_level, retention_policy, addr)` 中，前三个参数要求常量。按 [ACLE 数据预取定义](https://arm-software.github.io/acle/main/acle.html#data-prefetch) 分别解码：

| 参数 | 编码与意图 |
|---|---|
| `access_kind` | `0` 为 PLD/读，`1` 为 PST/写 |
| `cache_level` | `0` 为 L1，`1` 为 L2，`2` 为 L3；当前规范还定义 `3` 为 SLC，需核对目标与工具链支持 |
| `retention_policy` | `0` 为 KEEP/时间性，`1` 为 STRM/流式 |

例如 `__pldx(0, 1, 0, addr)` 表达 L2 读、KEEP 意图；`__pldx(1, 0, 1, addr)` 表达 L1 写、STRM 意图。`cache_level=1` 与 GCC `locality=1` 不是同一种参数；也不能把 ACLE 的三个参数编码当成 `PRFM #imm` 的整体编码。

`__pld(addr)` 表达最内层数据读预取；`__plix(cache_level, retention_policy, addr)` 用于指令预取，没有 `access_kind` 参数。ACLE 允许不支持的目标忽略层级或保留参数，预取 intrinsic 或硬件指令也可能无效果；源码参数只支持意图判读，是否发出对应指令仍需目标产物。

需要更多 ACLE 内联函数示例时，查阅 `arm-instructions-query` 技能的 `references/ch09-prefetch-intrinsics.md`；本技能负责把参数、条件分支和调用/消费位置关联成策略证据。

## 权威依据

- [Arm A64 PRFM 指令条目](https://developer.arm.com/documentation/ddi0602/2026-03/Base-Instructions/PRFM--literal---Prefetch-memory--literal--)：PLD/PST/PLI、层级及 KEEP/STRM 的定义；效果由具体实现定义。使用 immediate/register 形式时还应核对对应寻址条目。
- [Intel SDM 卷 2](https://cdrdv2-public.intel.com/835757/325383-sdm-vol-2abcd.pdf)：查询 `PREFETCHh`、`PREFETCHW` 等条目；区分 hint 意图和具体实现。
- [GCC Other Builtins](https://gcc.gnu.org/onlinedocs/gcc/Other-Builtins.html)：查询 `__builtin_prefetch`，核对版本、locality、目标不支持时的处理及地址求值约束。
- [Arm ACLE Memory prefetch intrinsics](https://arm-software.github.io/acle/main/acle.html#memory-prefetch-intrinsics)：核对 `__pldx`、`__plix` 的参数顺序、编码和不支持目标的处理。
- [Arm SVE PRFW 条目](https://developer.arm.com/documentation/111108/2026-03/SVE-Instructions/PRFW--vector-plus-immediate---Gather-prefetch-words--vector-plus-immediate--)：向量地址预取的示例；其他元素大小和寻址方式需读对应条目。

以上是语义出处，不是待分析程序的证据。离线环境无法核对新增/不熟悉指令时，保留原始操作数并列为待核实。
