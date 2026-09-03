# 候选优化点映射（R0-R7 / A-E / 已验证与待评估）

候选点按「价值 × 可行性」排序，作为阶段 2 候选池的**静态参考种子（非硬性要求）**。实际入选必须结合阶段 1 的动态证据（graph_profile × baseline_profile × cross_validation），可增删、可偏离本目录；本目录未列出的优化点，只要动态证据支持同样可进入候选池。

## 已验证优化点（详见 `references/optimization-work-summary.md`，供参考复用）

| ID | 名称 | 类型 | 已验证收益 | 备注 |
|---|---|---|---|---|
| A | Tensordot 折叠 | graph | op 累计 +1.6% | MatMul→Reshape→BiasAdd 折叠为 `_FusedMatMul` |
| C1 | MatMul→BiasAdd 融合 | graph | BiasAdd 1.17ms→3.4μs，端到端≈0 | 轻量 dispatch，端到端收益有限 |
| D | QKV 投影拼合 | graph+kernel | op 级 MatMul -57% / BMM -65%；端到端 P99 -5.4%~7.1%、CPU -2~2.5pp；容量 +20%~23% | grouped GEMM + per-role bias，计算密集，端到端有效 |
| E | LSTM gate 融合 | graph+kernel | op 累计 +4.7% | Sigmoid/Tanh→Mul 融合，复用 Eigen functor 保证 bit 级等价 |
| H | 数据搬运融合 | graph+kernel | 端到端≈0（负样本） | LSTM cell concat+split 融合，op 级有效但端到端≈0 |

## 待评估候选点（映射自 R0-R7）

| ID | 名称 | 类型 | 难度 | 优先级 |
|---|---|---|---|---|
| R0 | 八分片 embedding 合并 | graph | 中 | P1（需客户导出侧或 Grappler pass） |
| R1 | 窄 Gather 裁剪 | graph | 低 | P2（仅删 21 个非 MatMul 节点，低风险清理） |
| R2 | QKV 融合 | graph+kernel | 中 | 高（已验证为 D，见上） |
| R3 | fused attention | kernel | 高 | 低（FLOPs 仅 ~1.1%，收益主要在 layout） |
| R4 | fused BiLSTM | kernel | 高 | 低（占矩阵 FLOPs 13.5%，实现成本极高） |
| R5 | BN/LayerNorm/FFN 融合 | graph+kernel | 中 | 高（BN 折叠 + LayerNorm ARM 路径 + FFN prepack） |
| R6 | 业务 lookup primitive | graph | 中 | P1（依赖客户自定义 op 源码） |
| R7 | fetch 裁剪 + 冻结 + prepack | 工具链 | 低 | P0（transform_graph + freeze_graph，不改 C++） |

## 阻塞项（不进循环）

| ID | 名称 | 阻塞原因 |
|---|---|---|
| B | 自定义特征算子 fused-kernel | 客户自定义 op 源码缺失，无法获取 |

## 风险项（需显式授权）

| ID | 名称 | 风险 |
|---|---|---|
| C2 | fused attention | 数值/语义复杂，正确性风险 |

| F | residual FMA | 浮点重排导致正确性用例比对失败风险 |
