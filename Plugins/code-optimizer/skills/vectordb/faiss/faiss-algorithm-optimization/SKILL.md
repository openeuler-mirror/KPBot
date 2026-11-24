---
name: faiss-algorithm-optimization
description: 为 Faiss 算法优化选择领域技能，并将实测热点转为有适用条件、成本解释和回退方式的候选。适用于跨算法方法选择和候选评估。
---

# Faiss 候选规划方法

## 输入

索引类型、源码与数据布局证据、热点占比、冻结条件、正确性目标和可用实验记录。

## 执行步骤

1. 按 [算法路由](references/algorithm-routing.md) 使用对应领域技能。
2. 将热点分为计算、访存、分支、调度或结果维护，提出可被实验支持或否定的机制假设。
3. 用热点占比估算收益上限，评估 guard、额外维护成本、代码体积和数值风险。
4. 选择一个主要机制，明确特性开关、回退、UT/ST 和正式性能对照；候选失败也保留证据。

## 输出

候选排序、选择理由、实施边界及验证/停止条件。

## 约束

不以检索参数、精度、工作量或测试口径变化替代实现优化。参考记录须核对适用条件；假设不等于已验证收益。

## 参考资料

- [方法选择](references/optimization-method-map.md)
- [候选检查项](references/candidate-evaluation.md)
- [待验证方向](references/optimization-backlog.md)
