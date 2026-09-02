# TF 推理性能优化工作总结（脱敏案例）

> 脱敏自优化工作记录（模型名/IP/路径/分支名占位化）。作为 TF 纯 CPU 推理优化的端到端参考案例。

## 一、背景与目标

- 优化目标：`<MODEL_NAME>` 精排模型（3997 节点 SavedModel）的推理性能。
- 平台：鲲鹏 aarch64，自研 TF 2.20 fork（集成 KDNN + ANNC 加速后端）。
- 实测基线：baseline 17.42ms/请求 → opt（ANNC+KDNN）14.68ms/请求，1.19× 加速。
- 约束：正确性用例必须全部通过（业务硬约束，口径由用户定义）。

## 二、已完成优化点

### 1. Tensordot 折叠（A）✅

- 内容：Grappler Remapper 规则，`MatMul → Reshape → BiasAdd` 折叠为 `_FusedMatMul`。
- 收益：165 个未融合 MatMul → 0，BiasAdd 165 → 0，op 累计 +1.6%。

### 2. QKV 投影拼合（D）✅ grouped GEMM 方案

- 内容：grouped-GEMM + per-role bias，Q/K/V 三个独立投影融合成单个 kernel。
- 图匹配：融合 3 处（1 listwise + 2 VCG），无 QK/PV 误融合。
- op 级：`MatMul` -57%、`BatchMatMulV2` -65%，净省 ~1.6ms/req。
- 端到端：P99 -5.4%~7.1%、CPU -2~2.5pp；容量 +20%~23%（~310 → ~370-380 QPS）。

### 3. LSTM gate 融合（E）✅

- 内容：`_FusedSigmoidMul` / `_FusedTanhMul`，复用 Eigen functor 保证 bit 级等价。
- 收益：op 累计 +4.7%。

### 4. MatMul→BiasAdd 融合（C1）✅（轻量 dispatch 负样本）

- 收益：BiasAdd 1.17ms→3.4μs，**端到端≈0**。

### 5. 数据搬运融合（H）⚠️ 已尝试，端到端≈0

- LSTM cell concat+split 融合：op 级 concat/split 下降，但净收益仅 ~37μs，端到端≈0。

## 三、核心教训

1. **计算密集 GEMM vs 轻量 dispatch**：数据搬运/C1 这类「轻量 op dispatch 优化」op 级有效但端到端≈0；与 QKV（计算密集 GEMM，端到端有效）的本质区别在于是否省「真实计算」而非「调度开销」。
2. **A 与 QKV 有 MatMul 重叠**：A 可融合的 MatMul 从 15 降到 9（QKV 先吃掉 6 个），叠加收益 < 简单相加是正常现象。
3. **执行顺序**：QKV 在 graph_optimizer 层、A/E 在 remapper 层，无 matcher 失效。

## 四、收益汇总（精简）

- 三优化叠加（A+E+QKV）vs opt 基线：op 累计 -9.3%；端到端延迟 -4%~-9%、CPU -2.9~-3.8pp。
- 容量上限：baseline ~310 QPS → merge ~370-380 QPS（+20%~23%）。
- 限 QPS 场景「CPU 降、延迟持平」是正常现象（省的是 CPU 计算量 → 可支撑更高 QPS）。

## 五、下一步候选

1. 特征算子 fused-kernel（阻塞客户源码）。
2. fused attention（GEMM 深度优化）。
3. MatMul+BN+activation 空档（residual FMA 具体化）。
4. malloc/绑核系统层收尾。
