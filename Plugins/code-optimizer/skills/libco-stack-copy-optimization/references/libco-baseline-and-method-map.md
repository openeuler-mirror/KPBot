# libco 基线、方法地图与文件地图

## 1. 基线口径

- 上游基线：Tencent libco 开源版本。
- **参考实现是 AArch64 派生版，不是纯上游 master**：上游 `coctx_t` 非 i386 只有 14 个槽，
  且没有 `dregs[8]`、`co_free_sharestack` 和本方法集的拷贝内核。参考实现使用
  `regs[32]` + `dregs[8]` + 共享栈清理接口；应用到上游前需先应用 AArch64 派生补丁。
- 本方法集不绑定具体提交号；需要追溯代码来源时，以目标仓库历史为准。
- 实践分为两个阶段：
  1. **AArch64 适配**：目录/构建适配、上下文切换、栈管理、`d8-d15` 保存。
  2. **共享栈拷贝优化**：save/swap、对齐、分块、余数、调度与 tier 调优。

## 2. 方法地图

父入口与参考地图；方法子 skill 的接入状态见父 `SKILL.md` 路由表。

| 阶段 | 方法类别 | 解决的问题 |
|---|---|---|
| AArch64 适配 | 上下文切换 | 整数寄存器、SP、16B 栈对齐、首次启动 |
| AArch64 适配 | callee-saved SIMD | double/SIMD 跨 yield 串值 |
| 共享栈拷贝 | save/swap 语义 | 换出/换入方向与缓存污染 |
| 共享栈拷贝 | 64B 对齐 | 目标对齐与前缀处理 |
| 共享栈拷贝 | NEON 分块 | 块循环、寄存器分组、块完整性 |
| 共享栈拷贝 | 余数 | 32/64/96B 与 1–127B 尾拷贝 |
| 共享栈拷贝 | 指令调度 | load 延迟、依赖链、寄存器压力 |
| 共享栈拷贝 | tier 调优 | 按尺寸分布选择块大小 |
| 全流程 | 验证 | 逐字节、哨兵、ABBA、目标指标映射 |

## 3. 当前实现

### 上下文切换

- 保存 x0-x30 + SP。
- 保存/恢复 **d8-d15**（v8-v15 低 64 位，AAPCS64 callee-saved）。
- `coctx_t` 增加 `dregs[8]`，偏移 272；`coctx_make` 清零。
- `coctx_make` 16B 栈对齐，`get_sp` 获取 SP。

### 共享栈拷贝

- save/swap 两个入口：save `ldp+stnp`，swap `ldnp+stp`。
- 目标 64B 对齐 + 前缀余数。
- 两个块内核：128B（`<=1KB`）、384B（`>1KB`）。
- 余数：32/64/96B 内联 + 1–127B 首尾重叠。
- 非 AArch64 平台使用 libc `memcpy`。

```text
<128B  -> memcpy_rem
<=1KB  -> 128B block
>1KB   -> 384B block
```

## 4. 文件地图

| 文件 | 作用 |
|---|---|
| `lib/coctx.h` | `coctx_t`：`regs[32]` + `ss_size` + `ss_sp` + `dregs[8]` |
| `lib/coctx.cpp` | `coctx_make` / offset 断言 / dregs 清零 |
| `lib/coctx_swap.S` | AArch64 整数/SP + d8-d15 保存/恢复 |
| `lib/co_routine.cpp` | `co_swap`、`save_stack_buffer`、共享栈集成 |
| `lib/co_memcpy.c` | save/swap 入口与 128B/384B 分发 |
| `lib/memcpy_def.h` | 128B/384B 宏、余数、save/swap ld/st 选择 |
| `lib/memcpy_neon_impl.h` | 128B/384B NEON 内核 |

## 5. 设计取舍

- 参考实现保留 128B + 384B 两档；256B/512B/1024B/1536B 与 3-tier/6-tier
  属于历史实验，不作为默认档位。
- 目标应用按尺寸分布选择档位；块内核必须覆盖完整块。
- 非 AArch64 平台使用 libc `memcpy`。

## 6. 验证要点

- 上下文切换：d8-d15、整数/SP、16B 对齐、首次启动路径。
- 拷贝：全尺寸/对齐/哨兵/逐字节；save/swap 双路径。
- 性能：目标机、绑核、ABBA、中位数与分布；参考实现数据不作为目标应用结论。
