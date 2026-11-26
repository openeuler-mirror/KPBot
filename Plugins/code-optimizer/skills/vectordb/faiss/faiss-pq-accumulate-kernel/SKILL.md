---
name: faiss-pq-accumulate-kernel
description: 改写 Faiss pq4/QBS 类 4-bit 查表累加核：字节域半字节拆分、成对加宽累加免回偏、qbs 分组映射与寄存器预算、尾段与 scaler 交互。适用于 FastScan/QBS 扫描的查表累加热点，且能给出数值等价证明时；8-bit 逐码路径见 faiss-pq-group-scan。
---

# Faiss pq4 累加核改写

## 输入

nsq/M2、qbs/bbs 布局、scaler 模式（是否带 norm 缩放尾段）、实际内核与编译 flags、metric、采样中的累加链证据（指令构成、spill、每元素成本）。

## 执行步骤

1. 确认累加语义：低/高半字节各自的 LUT lane、字节进位与偏置修正、scaler 尾段的偏置与缩放；写出当前数值契约。
2. 判定 ISA 能力：是否支持字节域移位（免 u16 拆分）、成对加宽加法（免回偏修正）；等价性义务见 [累加等价性](references/accumulate-equivalence.md)。
3. 按 [寄存器预算](references/register-budget.md) 选择 NQ 与累加器布局；256-bit 抽象在窄寄存器 ISA 上占用双倍寄存器，直接决定可行分组。
4. 按 [qbs 映射](references/qbs-mapping.md) 选择分组形态；不同 SIMD 层的最优映射不同，须逐层实测。
5. 覆盖尾段：奇数 nsq、scaler 尾段、非整块；写 guard 与通用回退后进入冻结对照。

## 输出

改写方案、数值等价说明、寄存器预算表、qbs 选择依据、guard/回退与验证矩阵。

## 约束

- 不改变 LUT 含义、码字布局与候选顺序；整数累加范围与浮点顺序须证明不变。
- 加宽累加仅在无偏置、无缩放尾段的路径成立；带 scaler 的段必须保留原修正或单独证明。
- qbs 映射不跨层照搬；寄存器压力以实际反汇编与 spill 为证。
- 更宽向量类型本身不是收益依据，机制收益以冻结对照判定。

## 参考资料

- [累加等价性](references/accumulate-equivalence.md)
- [寄存器预算](references/register-budget.md)
- [qbs 映射](references/qbs-mapping.md)

方案选择与止损用 `faiss-optimization-selection`；层级与分派问题转 `faiss-simd-layer-port`；执行对照用 `faiss-benchmark-run`（尚未并入子域时按 `faiss-benchmark-baseline` 的对照口径执行并记录来源）。
