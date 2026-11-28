# 未验证模型：不做适配承诺

以下模型没有在本 skill 的验证范围内确认。不要根据模型名直接给出“支持/不支持”的结论。

| 模型/场景 | 当前状态 | 说明 |
|---|---|---|
| DeepSeek V3 / R1 / V3.1 | 未验证 | MLA 结构，理论上可作为候选，需检查 cache key、MTP 和拓扑 |
| Kimi K2 / K2.5 / K2.6 | 未验证 | 需按通用 gate 采集 |
| DeepSeek V3.2 | 未验证 | DSA/indexer 可能触发多 entry / 页大小问题 |
| GLM-5 / 5.1 | 未验证 | DSA + index cache，需检查 page-class / shared indexer |
| Qwen3 系列 | 未验证 | dense/GQA/hybrid 路径不同，需逐项检查 attention hook |
| Llama / GQA 类 | 未验证 | 若 attention 后端缺少逐层 hook，不要直接开 P2P layerwise |
| 其他新模型 | 未验证 | 先跑通用准入检查，不写适配承诺 |

## 使用规则

- 只把这些当作“可能可以”的线索，不作为准入结论。
- 必须采集 `kv_caches.keys()`、`num_kv_cache_groups`、`num_draft_attn_layers`、`attn_backend`。
- 需要修改 connector/planner 才能支持的结论，超出本 skill 范围，交 `code-optimizer`。
