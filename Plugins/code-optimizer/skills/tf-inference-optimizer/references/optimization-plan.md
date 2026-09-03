# TF 2.20 源码优化方案（脱敏案例）

> 脱敏自优化方案记录。提供 R0-R7 优化点与 TF 2.20 源码位置的映射，作为候选池的静态种子。

## 一、源码现状关键事实

| 项 | 状态 |
|---|---|
| TF 版本 | 自研 2.20 fork（华为修改版，KDNN 集成于 `third_party/KDNN`） |
| KDNN 算子覆盖 | MatMul(gemm/inner_product)、BN、LayerNorm、RNN、Softmax、Concat、Pooling 等 |
| 现有 KP fused ops | `KPFusedGather`、`KPFusedSparseSegmentReduce`、`KPFusedSparseDynamicStitch` 等（NEON intrinsics） |
| 现有标准 fused ops | `_FusedMatMul`、`_FusedBatchNormEx`（LayerNorm/attention 无 ARM 路径） |

## 二、R0-R7 优化项与代码位置

| 方案 | 代码位置 | 难度 |
|---|---|---|
| R0 八分片 embedding 合并 | 训练导出脚本 + `grappler/optimizers/custom_optimizer.cc` | 中 |
| R1 最终选择路径窄 Gather | 训练导出脚本 | 低 |
| R2 QKV 融合 | `grappler/optimizers/qkv_fusion.cc`（新建） | 中 |
| R3 fused attention | `core/kernels/kp_fused_attention_op.cc`（新建） | 高 |
| R4 fused BiLSTM | `core/kernels/kp_fused_bilstm_op.cc`（新建） | 高 |
| R5 BN/LayerNorm/FFN 融合 | `core/kernels/kp_layernorm_op.cc`（新建，ARM 路径） | 中 |
| R6 业务 lookup primitive | `python/ops/kp_embedding_lookup.py` + `grappler/optimizers/kp_lookup_fusion.cc` | 中 |
| R7 fetch 裁剪 + 冻结 + prepack | 工具链（transform_graph/freeze_graph） | 低 |

## 三、关键方案要点

### R2 QKV 融合
识别 3 个 MatMul 共享同一输入 → 合并为宽 MatMul + Split，或 grouped GEMM（KDNN batched MatMul）。风险：对 inter-op 并发度有影响，需 A/B 验证 worker 利用率。

### R5 BN/LayerNorm/FFN 融合
- R5a：BN 折叠到 Dense 权重（离线 `W'=W*scale; b'=(b-mean)*scale+beta`），伪 ReLU `(x+abs(x))/2` 改标准 ReLU。
- R5b：ARM LayerNorm 新写（`_MklLayerNorm` 仅 Intel），走 KDNN layer_normalization 或 NEON。
- R5c：Dense `MatMul+BiasAdd+activation` 映射 KDNN post-op。

### R7 工具链（不改 C++）
`transform_graph` 按 fetch 删死分支 + `freeze_graph` 冻结 + KDNN prepack 自动生效。

## 四、推荐实施路线（价值排序）

| 阶段 | 工作项 | 优先级 |
|---|---|---|
| 0 | 补齐客户阻塞（variables/签名/自定义 op/样本） | P0 |
| 1 | R7 fetch 裁剪 + 冻结 + R5a BN 折叠（Python 重导出） | P0 |
| 2 | R6 KP fused lookup 接入 | P1 |
| 3 | R0 八分片 embedding 合并 | P1 |
| 4 | R2 QKV 融合 | P2 |
| 5 | R5c ARM LayerNorm | P2 |
| 6 | R3 fused attention / R4 fused BiLSTM | P3 |
