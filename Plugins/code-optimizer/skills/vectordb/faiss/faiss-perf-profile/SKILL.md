---
name: faiss-perf-profile
description: 采集 Faiss CPU 查询的性能计数与调用图，将热点映射到实际内核、源码和汇编。适用于基线建立后的扫描、距离、量化、候选容器、调度或访存瓶颈调查。
---

# Faiss 性能采样

## 输入

可复现的查询命令、构建/库身份、数据与参数、CPU/NUMA 约束和待验证性能问题。

## 执行步骤

1. 检查采样权限、调试信息、调用栈可用性和冻结条件；确认实际加载的库。
2. 一次采一个明确工作负载，使用可用的 perf 事件采集计数和调用图，记录频率、事件、样本分母及丢失情况。
3. 区分加载/构建、查询与结果统计；将 self/inclusive 占比、调用方、源码和汇编关联。
4. 用数据检验计算、访存、分支、寄存器或调度假设，说明覆盖范围和未解决的归因。

## 输出

`PROFILE-SUMMARY.md`、原始 perf 数据/命令、热点表、源码映射和不确定性说明。

## 约束

采样开销下的吞吐量不作为正式对照。全局 cache/branch 计数不能直接证明某条指令或某个函数的瓶颈；跨任务的热点占比不能复用为当前实测。

## 参考资料

- [命令与证据](references/perf-command-guide.md)
- [工作负载选择](references/profile-matrix.md)
- [热点归因](references/hotspot-attribution.md)
