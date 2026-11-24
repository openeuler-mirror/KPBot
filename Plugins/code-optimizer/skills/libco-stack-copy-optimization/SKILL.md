---
name: libco-stack-copy-optimization
description: >
  用于 libco 共享栈切换的栈拷贝优化。当切换时 save/restore 拷贝占比高、
  目标地址未对齐、块循环或小尺寸尾部成为热点时使用。
  其他栈式协程应用仅在具备共享栈/copy-stack 特征时作为次级参考。
  触发词：libco、共享栈、栈拷贝、copy-stack、栈换出换入。
  不适用：非栈式协程、普通一次性 memcpy、内核/OS 调优。
---

# libco 共享栈拷贝优化

本 skill 服务 libco 及其衍生库；其他栈式协程应用只在具备共享栈/copy-stack 特征时参考。

## 1. 适用对象

- 默认目标：libco 共享栈切换中的栈 save/restore 拷贝。
- 次级目标：其他 copy-stack 协程库，且需要重新映射接口、阈值和验收指标。
- 上游 Tencent/libco 没有本方法集的拷贝内核；以下内容适用于带 AArch64 拷贝实现的派生版。

## 2. libco 文件与结构

| 文件/结构 | 作用 |
|---|---|
| `lib/co_memcpy.c` | save/swap 入口与块拷贝分发 |
| `lib/memcpy_def.h` | 块宏、余数、save/swap ld/st 选择 |
| `lib/memcpy_neon_impl.h` | NEON 块内核 |
| `lib/co_routine.cpp` | `save_stack_buffer` 与共享栈集成 |

完整基线、派生差异和文件地图见
[references/libco-baseline-and-method-map.md](references/libco-baseline-and-method-map.md)。

## 3. 使用流程

1. 确认共享栈 save/restore 的两个复用方向，分别建立拷贝基线。
2. 先修正确性：长度、对齐、前后哨兵、逐字节比对。
3. 再按尺寸分布选择对齐、分块和余数路径；本 skill 默认使用 1KB 阈值。
4. 用 ns/switch 或端到端指标验收，不用 direct copy 代替业务收益。

## 4. 子 skill

| 子 skill | 何时用 | 核心动作 |
|---|---|---|
| `libco-stack-copy-save-swap` | libco 共享栈换出/换入方向不清 | 区分复用方向并选择缓存策略 |
| `libco-stack-copy-align64` | libco 栈拷贝目标地址未对齐 | 前缀与块循环边界处理 |
| `libco-stack-copy-block-kernels` | libco NEON 块拷贝的块大小/完整性 | 按尺寸选块并保证块覆盖完整 |
| `libco-stack-copy-remainder` | libco 栈拷贝的尾部/小尺寸余数 | 位宽递减与首尾重叠处理 |

## 5. 验证与门禁

- 栈拷贝必须逐字节等价，覆盖长度/对齐/哨兵和 save/swap 双路径。
- 块内核覆盖完整块；384B 块后剩余 >=128B 时先消费 128B 子块。
- 小尺寸单独验证，不由大块结果外推。
- 没有目标应用基线、绑核和真实负载时，不得宣称性能改善。

## 共享参考

- [references/libco-baseline-and-method-map.md](references/libco-baseline-and-method-map.md)
- [references/other-applicability.md](references/other-applicability.md)
