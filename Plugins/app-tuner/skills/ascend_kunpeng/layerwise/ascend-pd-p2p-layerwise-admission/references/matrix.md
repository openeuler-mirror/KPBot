# 公开版 Mode A 支持矩阵

> 只记录有代码或社区证据的结论。具体机型、拓扑和测试数据不写入本 skill，以现场采集为准。

| 模型/场景 | P2P Layerwise Mode A | 说明 | 证据类型 |
|---|---|---|---|
| GLM-5.2 W8A8C8 / GLM-5.3-W4A8 | 有条件支持 | 优先 Mode A；检查同 layer index 是否多 cache key，以及 checkout 是否包含公开版能力 | 社区/现场线索，需按现场复核 |
| 带独立 indexer/tail cache 的 DSA | 需检查同 index 多 key | 若同一物理 layer index 出现多个 `kv_caches` key，当前 connector 可能断言 | 代码约束 |
| 标准 MLA / 单 cache key | 通常可作为候选 | 先跑 G1/G2/G4，再进入配置 | 代码约束 |
| hybrid / Mamba 多 group | 需检查 connector 能力 | 公开 Mode A 只承诺 Store 非 layerwise；不要扩展到 Store layerwise | 代码约束 |
| 未验证模型 | 未验证 | 跑通用准入，不做承诺 | 无证据 |

## 判定口径

- “P2P 支持”：attention 后端有 wait/save hook，且每个物理 layer index 只有一个 `kv_caches` key，或 connector 支持 component-aware 多 entry。
- “公开 Mode A”：Store `use_layerwise=false`，P2P 使用 `MooncakeLayerwiseConnector`。
- Store layerwise range、host offload、内部 fork 字段不在本矩阵范围内。
- 未验证模型见 `unverified-models.md`；不要根据模型名直接下结论。
