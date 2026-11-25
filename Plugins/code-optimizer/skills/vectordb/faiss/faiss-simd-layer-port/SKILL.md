---
name: faiss-simd-layer-port
description: 为 Faiss 增加或修正定宽 SIMD 层（如 256-bit SVE）：类型层构造、层级挂载与运行时分派、能力与向量长度门控、单层与动态分发两种构建语义、按翻译单元的编译属性与显式实例化。适用于目标 ISA 未进入产物、分派未命中或需要新增向量层级时；NEON/SVE 内核选型与实际启用证据见 faiss-simd-optimization。
---

# Faiss 定宽 SIMD 层移植

## 输入

目标 ISA 与向量长度、编译器能力、当前层级组织（类型特化、selector、分派掩码、构建模式）、实际产物与加载库证据、正确性要求。

## 执行步骤

1. 定位现状：类型特化挂载点、256/512-bit selector 映射、分派掩码与 `with_simd_level` 入口、单层/动态分发构建模式；以反汇编与实际加载库确认真实路径，不以宏或头文件存在为准。
2. 定宽 vs 可变：在长度可变的向量 ISA 上复用固定宽度抽象，需要编译器定宽支持与逐元素谓词；类型层构造见 [类型层](references/layer-anatomy.md)。
3. 挂载与分派：selector 直通或降级、新增分派掩码、专用 `with_simd_level` 变体；见 [分派与门控](references/dispatch-and-gating.md)。
4. 门控：能力位 + 代次 + 向量长度精确匹配，且校验发生在实际执行内核的线程上（VL 按线程维护，进程级选层不能替代）；不匹配一律走基线实现；谓词写成纯函数便于单测；分派入口与回退实现按基线 ISA 编译；单层产物限定兼容主机运行，不兼容主机先选独立编译的基线产物。
5. 构建与实例化：按 [构建与实例化](references/build-tu-and-instantiation.md) 设置逐翻译单元 flags、集中显式实例化、`extern template` 防跨翻译单元误实例化。
6. 验证矩阵：分别覆盖向量长度不匹配（偏小/偏大）与目标 ISA 缺失——门控不命中、回退路径反汇编无定宽指令；覆盖同进程多线程持不同 VL 时各线程选层正确（含仅初始化线程校验过的负例）；另覆盖单层与动态分发两种构建、未实现特化的链接完整性。

## 输出

层级方案、挂载与门控清单、构建清单（flags 归属与实例化）、回退路径与验证矩阵。

## 约束

- 宏名、头文件或编译器能力都不等于层级已启用；证据纪律（反汇编、采样、加载库核对）沿用 `faiss-simd-optimization` 技能的实际启用证据参考。
- 更宽向量不是收益依据；寄存器预算与内核形态由各方法技能判定。
- 逐翻译单元架构 flags 泄漏到含基线代码的翻译单元时，必须核对自动向量化不产生越界指令。
- 单层常量依赖编译上下文取值，跨翻译单元合并是错误来源。

## 参考资料

- [类型层](references/layer-anatomy.md)
- [分派与门控](references/dispatch-and-gating.md)
- [构建与实例化](references/build-tu-and-instantiation.md)
- [改写风险](references/rewrite-risks.md)

内核选型决策（NEON vs SVE、何时值得更宽向量）与 ARM 源码导航按技能名调用 `faiss-simd-optimization`；方案选择与止损用 `faiss-optimization-selection`。
