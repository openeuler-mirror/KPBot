# 两个重排模型的静态计算图分析（脱敏案例）

> 脱敏自静态图分析记录（模型名/路径占位化）。展示 saved_model.pb 静态图分析的方法论与结论框架，是 `tf-profile-collector/references/static-graph.md` 的实证来源。

## 一、分析边界与证据分级

- **方法**：只解析 SavedModel protobuf，不加载 TF Session、不执行自定义 op（缺 op 库也能分析）。
- **证据分级**：

| 等级 | 含义 | 示例 |
|---|---|---|
| 已确认 | 直接来自 GraphDef/节点属性/静态 shape | 节点数、MatMul 数、权重 shape |
| 强推断 | 多结构+命名共同支持，缺业务契约 | 多目标重排、最终选择 |
| 待验证 | 依赖真实输入/变量/运行时 profile | 实际热点、缓存命中率、QPS/P99 |

- 静态分析**不能**证明某算子就是运行时瓶颈，需结合 trace/perf 量化。

## 二、两个模型静态画像

| 指标 | `<MODEL_A>`（embedding 主导） | `<MODEL_B>`（计算主导） |
|---|---|---|
| 全图节点 | 1,044 | 3,997 |
| 活跃节点 | 447 | 2,230 |
| 可裁剪比例 | 57.2% | 44.2% |
| 活跃 MatMul/BMM | 16 / 0 | 28 / 10 |
| 变量逻辑体量 | ~8 GiB | ~96 MiB |
| 静态矩阵 FLOPs/请求 | ~1.2 M（M=1） | ~1.29 G |

**关键结论**：
- `<MODEL_A>` 是 8 GiB 哈希 embedding 主导 → 随机访问延迟/TLB/NUMA/内存带宽是高价值点。
- `<MODEL_B>` 是 1.29 GFLOPs 计算主导（BiLSTM + 3 组 attention + MLP）→ 图融合与 KDNN 计算优化空间更大。

## 三、静态分析能回答什么

1. **计算维度**：MatMul/BMM 数量与 shape、静态 FLOPs、权重参数体量。
2. **融合机会**：QKV 投影、LSTM gate、BN/LayerNorm、分片 embedding lookup、伪 ReLU 等模式识别。
3. **架构理解**：分层 mermaid 图（预处理/embedding/主干/塔/输出）。
4. **裁剪空间**：非输出祖先闭包节点比例（导出体积/加载/图优化机会，非请求耗时收益）。

## 四、活跃图特征（`<MODEL_B>` 示例）

| Op | 数量 | 含义 |
|---|---|---|
| GatherV2 | 124 | 92 数据 lookup + 30 shape plumbing |
| ConcatV2 | 65 | 特征拼接/输出组装 |
| Transpose | 32 | attention/layout 转换，可能真实搬运 |
| MatMul / BMM | 28 / 10 | Dense/LSTM/投影/任务头 |
| 分片 lookup | 9 组×8 | 72 分片 Gather + 18 DynamicPartition + 9 Stitch |

## 五、验证工作流（正确性/性能门槛）

- **正确性**：固定 100-1000 条脱敏请求，按用户定义的正确性用例判据比对（如逐元素误差 + 一致率，口径由用户确认）。
- **性能**：固定 `inter=16/intra=16` + 同 cpuset/NUMA policy；QPS/P50/P90/P99 + 热点 op 调用数/耗时 + LLC miss/带宽/NUMA。
