# 工作流门控 + 候选池决策树 + 报告结构

## baseline 认知（易错，贯穿全流程）

- baseline = **本轮优化的对照起点**，即「当前已合入的最新优化状态」，不是「无优化」的同义词。
- 带加速 flag 的版本（`--enable_kdnn=true --annc=true` 等）是**已优化后的状态**，它就是进一步优化的 baseline；不要认为「带 baseline flag 才是 baseline、带 opt flag 就是 opt」。
- 无加速 flag 的版本是「原生基线」，仅首次评估整体加速比时对照使用。
- 判断标准：**看「当前已合入了哪些优化点」，不看 flag/binary 名叫 baseline 还是 opt**。

## 四硬门控

| 门控 | 位置 | 阻断性 | 条件 |
|---|---|---|---|
| GATE① 环境就绪 | 阶段0 → 阶段1 | 阻断 | 环境信息已记录、链路通、kernel 符号非 0、无残留进程、server ready |
| GATE② 基线已建立 | 阶段1 → 阶段2 | 阻断 | 已产出当前 baseline 的容量/P99/CPU 基线数据（端到端极限测试），写入 baseline_state.json |
| GATE③ profiling 已确认 | 阶段2 → 阶段3 | 阻断 | profiling_report.md（方法/结果/分析）+ graph/baseline/cross_validation 三份 JSON 落盘 + 用户 review 确认 profiling 结论与 baseline（「当前已优化状态」） |
| GATE④ 正确性用例 | 阶段3 每个优化点 | 逐点回退 | 正确性用例（口径由用户定义）全部通过，不过即回退该点（不阻断其他点） |

> 顺序不可颠倒：**先建立基线（极限测试），再 profiling，再优化**。跳过基线直接 profiling/优化，会导致最终没有对照基准、收益无法归因。

## 候选池决策树

```
┌ 计算密集 GEMM（QKV/MatMul/LSTM gate/BN 折叠）→ 高优先级（端到端有效）
├ 轻量 dispatch（concat/split/transpose 消除）    → 低优先级（op级有效端到端≈0，负样本 H）
├ 阻塞项（客户自定义 op 源码缺失）                → blocked，不进循环
└ 高风险（fused attention / 正确性敏感）          → 需显式授权
```

核心经验：**优先「计算密集 GEMM」>「轻量 dispatch 优化」**。数据搬运/C1 这类轻量 op dispatch 优化 op 级有效但端到端 ≈0；与 QKV（计算密集 GEMM，端到端有效）的本质区别在于是否省「真实计算」而非「调度开销」。

## 报告结构（OPTIMIZATION_WORK_SUMMARY.md）

```
1. 背景与目标（含初始 baseline 基线 + 正确性用例硬约束）
2. 已完成工作（逐优化点：内容 / 收益 / commit / 轮次）
3. 优化点全景与价值分析（含 blocked / 负样本 / 待做）
4. 实测收益汇总（op 累计 + 端到端 + 容量 三表）
5. 当前状态（滚动 baseline 终态）
6. 下一步计划（未做候选点 → 供下一轮候选池复用）
```
