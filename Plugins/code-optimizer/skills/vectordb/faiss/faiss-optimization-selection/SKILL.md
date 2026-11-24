---
name: faiss-optimization-selection
description: 在 Faiss 内核优化候选之间做选择：用热点信号匹配机制、估算可移除成本上界、确定 guard 与回退形态，并用噪声带与止损规则拒绝无效改动。适用于已有热点与候选清单、需要决定"先做哪个、值不值得做"的场景；机制实现细节由各方法技能承担。
---

# Faiss 优化方案选择

## 输入

热点事实（self 占比、调用次数、单次成本、计时范围）、源码与构建身份、冻结条件、候选清单或方向、正确性与召回要求、可用实验记录。

## 执行步骤

1. 事实闸门：确认真实执行路径（分派、宏、实际加载库）、热点归因（self/inclusive 与调用方）、可移除成本上界。任一缺失时只能提出假设，不得进入选择。
2. 机制闸门：按 [选择矩阵](references/selection-matrix.md) 将信号映射到机制族并路由到对应方法技能；一个候选只承载一个主要机制。
3. 形态闸门：确定落地形态——局部改写、新增 SIMD 层、接口两侧扩展、参数/算法变更（后两者须另立实验，参数变更不计入纯实现收益）。默认关闭开关、保留通用回退、阈值 guard；阈值必须来自实测拐点。
4. 收益止损：按 [收益与止损](references/benefit-and-stop.md) 估算上界与摊销，预声明噪声带与最小可检测差异；对照未超噪声带即拒绝，负结论保留。

## 输出

候选排序、选择理由、实施边界（guard/开关/回退）、验证与停止条件、被拒候选及拒绝依据。

## 约束

- 上界不是收益预测；可移除成本必须给出摊销条件。
- 调参、精度或工作量变化不计入纯实现收益。
- 静态汇编变化只证明实现形态，不证明性能收益。
- 候选失败同样保留证据；结论只对当前负载与条件负责，不回写为通用推荐。

## 方法技能索引

| 信号 | 技能 |
| --- | --- |
| 目标 ISA 未进入产物或分派未命中 | `faiss-simd-layer-port` |
| pq4/QBS 累加链、寄存器压力 | `faiss-pq-accumulate-kernel` |
| 逐码归约、查表访存 | `faiss-pq-group-scan` |
| LUT 打包、表足迹、setup 摊销 | `faiss-lut-layout` |
| 过滤分支、统计写、间接寻址 | `faiss-scan-loop-overhead` |
| 随机行访问 miss | `faiss-memory-latency` |
| 大量不可能胜出的候选 | `faiss-early-exit` |

注：表中承接技能若尚未并入子域、不可按技能名调用，按本表信号与形态口径直接执行机制并记录证据，并入后切换为按技能调用。

## 参考资料

- [选择矩阵](references/selection-matrix.md)
- [收益与止损](references/benefit-and-stop.md)

执行与证据：基线冻结用 `faiss-benchmark-baseline`，两臂执行用 `faiss-benchmark-run`，特征采集用 `faiss-feature-capture`（采样命令与归因用 `faiss-perf-profile`），统计与比较用 `vector-experiment-methodology`，测试设计用 `vector-ut-st-validation`。
