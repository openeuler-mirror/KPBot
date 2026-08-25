# saved_model.pb 静态图分析

静态解析 `saved_model.pb`，提取图的全部结构信息，尤其**计算维度**（FLOPs/权重 shape/MatMul 分布），是理解整个算子推理链的关键。

## 1. 方法选择（根据环境信息）

| 场景 | 工具 |
|---|---|
| 本地有 TF 库 | `tf.saved_model.load` / `tf.compat.v1.saved_model.loader.load` |
| 本地无 TF、只有 protobuf | 纯 `google.protobuf` 读 `saved_model.pb` → `MetaGraphDef` → `GraphDef` |
| 本地无任何环境 | `remote-exec.ps1` 远端解析 |

**关键约束**：不加载 Session、不执行自定义 op（客户自定义 op 动态库可能缺失）。只解析 protobuf，从 GraphDef 节点/边/shape/属性提取信息，避免因缺 op 库无法分析。

## 2. 解析步骤

```python
import tensorflow as tf  # 或纯 protobuf 解析
# 1. 读取 SavedModel
sm = tf.saved_model.load(<MODEL_DIR>)
# 2. 遍历 GraphDef 节点
graph_def = sm.signatures["serving_default"].graph.as_graph_def()  # 或直接读 pb
for node in graph_def.node:
    print(node.op, node.name, node.attr.get("T"), node.input)
```

## 3. 提取指标

### 3.1 节点统计
- 总节点数、输出祖先活跃节点、可裁剪比例（不在输出祖先闭包中的节点）。
- 活跃 op 数量分布（MatMul/BatchMatMulV2/GatherV2/ConcatV2/Transpose/Sigmoid/Tanh 等）。

### 3.2 计算维度（关键）
- MatMul/BMM 数量与 shape（矩阵 `[M,K]×[K,N]` 维度）。
- 权重 shape + 参数量（FP32 逻辑体量）。
- 静态 FLOPs 估算（按 batch=1 / 候选维度推算，标注为"架构估算"非 profiler 实测）。

### 3.3 融合机会识别
匹配以下可融合模式（对应候选优化点）：

| 模式 | 识别信号 | 候选点 |
|---|---|---|
| QKV 投影 | 3 个 MatMul 共享同一输入 | D/QKV |
| LSTM gate | Sigmoid/Tanh → Mul | E |
| BN/LayerNorm | Mean→Rsqrt→Mul/Add 链 | R5 |
| 伪 ReLU | `(x + abs(x)) / 2` | R5a |
| 分片 embedding | DynamicPartition→N Gather→DynamicStitch | R0 |
| MatMul→BiasAdd | MatMul 后接 BiasAdd | A/C1 |

### 3.4 架构理解
分层 mermaid 架构图（预处理 / embedding / 主干 / 塔 / 输出），标注主要 tensor shape。

### 3.5 融合命中/拒绝统计（前/后图 diff）

静态图分析要量化「哪些融合规则命中、哪些未命中、为什么」，而非只列「存在哪些模式」。对每条融合规则产出：

- **命中计数**：该规则实际可融合的位置数（如 QKV 投影 3 处）。
- **拒绝原因归类**：对每个未命中的候选节点，归类其 reject 原因（如 `attention_topology(forward)`、`width_mismatch`、`shared_input_missing`），统计各类占比。
- **前/后图 diff**：若已拿到「图优化开启/关闭」两版 GraphDef（或 matcher 的 `[*-MATCH]` 日志），对比融合前后节点数 / op 分布变化，量化融合实际吃掉了多少节点。

目的：让候选点优先级由「图结构量化证据」支撑，而非人肉经验判断。

## 4. 输出 `graph_profile.json`

```json
{
  "saved_model_pb": "<MODEL_DIR>/saved_model.pb",
  "total_nodes": 3997,
  "active_nodes": 2230,
  "prunable_pct": 44.2,
  "op_counts": { "MatMul": 28, "BatchMatMulV2": 10, "GatherV2": 124 },
  "matmul_bmm_shapes": [["[280,176]×[176,176]"], ["[70,512]×[512,512]"]],
  "static_flops_per_req": 1.29e9,
  "weight_params": 25182721,
  "fusion_patterns": [
    { "pattern": "QKV_projection", "loc": "listwise+VCG", "candidate": "D" },
    { "pattern": "LSTM_gate_sigmoid_tanh", "candidate": "E" },
    { "pattern": "partitioned_embedding_x8", "candidate": "R0" }
  ],
  "fusion_hit_reject": [
    {
      "rule": "QKV_projection",
      "hit_count": 3,
      "reject_reasons": { "attention_topology(forward)": 5, "width_mismatch": 2 },
      "graph_diff": { "MatMul": "28→17", "BatchMatMulV2": "10→4", "KPFusedQKVProjection": "0→3" }
    }
  ],
  "arch": "<mermaid 架构图>"
}
```

## 5. 与动态 trace 交叉

静态图给出「融合模式 + 计算维度」，动态 trace 给出「热点」。二者交叉才能定位「既热点、又可融合/可优化」的新优化点。单靠其一都不完整。
