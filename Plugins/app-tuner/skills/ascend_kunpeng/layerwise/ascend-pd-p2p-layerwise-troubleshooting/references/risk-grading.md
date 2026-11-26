# 公开版 Mode A 风险分级

严重度：高 = 启动失败 / 精度错误 / 请求挂死；中 = 特定场景失败或性能下降；低 = 预期差异或可观测性问题。

| ID | 风险 | 严重度 | 结论 | 验证 | 缓解 |
|---|---|---|---|---|---|
| R1 | 配置混入 `use_layerwise_range` / `layerwise_host_kv_offload` | 高 | 公开版不会按这些字段开启对应能力 | 检查配置与现场代码 | 改为公开 Mode A |
| R2 | `use_ascend_direct` 写进 `MooncakeLayerwiseConnector` 子项 | 中 | 该字段不属于该 connector 的公开选项 | 检查子项字段 | 删除或按 V2 文档配置 |
| R3 | `load_async` 写成字符串 | 高 | 非空字符串会被当 truthy | 启动日志/配置类型 | 保持 JSON boolean `false` |
| R4 | attention 后端缺少逐层 hook | 高 | P2P layerwise 不触发 | `attn_backend` | 换支持的 attention 路径 |
| R5 | 同 layer index 多 `kv_caches` key | 高 | connector 可能断言 | `kv_caches.keys()` | 合并 cache / 关 runtime indexer |
| R6 | draft layer 数量超出映射能力 | 高 | 只映射部分 draft | `index_to_name` | 保持单 draft 或扩展源码 |
| R7 | proxy/metaserver 超时或端口不一致 | 高 | D 可能等待远端 KV | D 状态与 proxy 日志 | 超时、健康检查、端口核对 |
| R8 | 量化 dtype 不一致 | 中 | 精度异常 | P2P/Store dtype 对比 | 固定 dtype |
| R9 | TP/PCP/DCP 映射不匹配 | 中 | 请求失败或错块 | 先同 TP、无 CP | 按支持组合逐步验证 |
| R10 | block/hash block 不一致 | 中 | 映射异常 | 打印 block 配置 | 统一 block 配置 |
| R11 | Mooncake 版本不匹配 | 中 | 传输失败 | 版本检查 | pin 到支持版本 |
| R12 | engine_id / 端口冲突 | 中 | 启动/连接失败 | 启动日志 | 每 rank 唯一 |
| R13 | hybrid / GQA 缺少逐层 hook | 高（该类模型） | 模型相关风险 | `attn_backend` / `kv_caches` | 用非 layerwise 或扩展源码 |
| R14 | prefix cache 改变传输范围 | 中 | 错块/缺块 | 唯一 prompt、关闭 prefix cache 对比 | 先无 prefix hit 验证 |
| R15 | 失败处理 / invalid block 未生效 | 中 | 请求挂起或带错误继续 | 失败注入 | 按策略 fail/recompute，监控 invalid block |
| R16 | Decode 非逐层消费 | 低 | 预期差异 | D 调度观察 | 调整性能预期 |
| R17 | 可观测性不足 | 低/中 | 难以定位 | metrics | 增加 send queue/source future 监控 |
| R18 | FirstWin + losing Mooncake double-write / block 数不一致 | 高 | 重复接收或错块 | 连接器顺序/FirstWin 日志 | 调整顺序或避免 AscendStore 赢得 load ownership |
| R19 | 后缀 `remote_block_ids` 的绝对索引越界 | 高 | `IndexError` 或错块 | 对比 remote 长度与 `d_block_idx` | 按 per-rank 前缀偏移；不要静默跳过 |
| R20 | 目标 checkout 与文档版本不一致 | 中 | 能力误判 | 现场版本/PR 门禁 | 以现场代码为准，不按模型名推断 |

## 优先处理顺序

1. R1/R2：先确认公开 Mode A 配置。
2. R3：修布尔类型。
3. R4/R5/R6：确认 P2P 逐层传输前提。
4. R7/R18/R19：处理超时、FirstWin 和 block 映射。
5. R8-R14：按模型/拓扑逐项验证。
6. R20：检查目标 checkout 与社区状态。
