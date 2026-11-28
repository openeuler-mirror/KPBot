# 公开版 Mode A 排障对照

| 现象 | 常见原因 | 处理 |
|---|---|---|
| P2P 不触发 / 请求超时 | 普通 P-first proxy、端口/engine_id 冲突、connector 未启用 | 换 D-first layerwise proxy，检查端口和 engine_id |
| Decode 卡在 `WAITING_FOR_REMOTE_KVS` | metaserver / 远端端口 / proxy 异常 | 检查 proxy、remote_host/port、metaserver 日志 |
| 同 layer index 多 `kv_caches` key | `.indexer.k_cache` / tail 等独立 key | 合并 cache、关 runtime indexer，或换支持多 entry 的 connector |
| draft layer 数量超出映射能力 | 多 draft layer | 保持单 draft，或交 `code-optimizer` 扩展 |
| 请求成功但输出异常 | 缺层、缺 block、indexer/tail cache 未传 | 做同一 prompt 的本地 vs PD 精度对比 |
| 配置含 `use_layerwise_range` / `layerwise_host_kv_offload` | 使用了内部/fork 字段 | 改为公开 Mode A；这些字段不属于公开 v0.30–v0.32/main |
| `use_ascend_direct` 写在 MooncakeLayerwiseConnector 下 | 该字段属于 Mooncake V2 路径 | 删除或按 V2 文档配置，不要放进 Layerwise 子项 |
| AscendStore 在前 + Decode store hit 时 P 侧 `IndexError` / double-write | losing Mooncake 仍收到 external tokens | 调整连接器顺序或避免 AscendStore 赢得 load ownership；按版本确认修复 |
| `consumer_is_to_load=false` 仍出现 `IndexError`，D 只发后缀 blocks | `get_transfer_mappings` 用绝对 `d_block_idx`，未减去前缀块数 | 关闭 Decode prefix cache / 用唯一 prompt 规避；或补源码按 per-rank 前缀偏移 |
| block 数不足但请求继续 | 映射异常被静默跳过 | 不应静默跳过；应收敛为失败并触发 D 侧 invalid block / 重算 |
| 失败后 D 仍使用旧 block | invalid block / 重算路径未生效 | 检查 `kv_load_failure_policy`、D 的 invalid block 日志和请求状态 |

## 关键代码区域

- `vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_layerwise_connector.py`：逐层 send/recv、`index_to_name`、MTP 映射。
- `vllm_ascend/distributed/kv_transfer/utils/utils.py`：`get_transfer_mappings` 与 block 下标。
- `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/`：Store 非 layerwise 的 KV pool 路径。
- `vllm_ascend/attention/`：逐层 wait/save hook。

具体文件路径可能随发布线变化；排障时以现场 checkout 为准，不把路径当作版本无关的硬依据。
