# 社区 layerwise 版本门禁

> 状态核对日期：2026-11-27。以下只记录对公开版能力判断有影响的 PR/issue；具体以现场 checkout 为准。

## 已合并

| PR | 状态 | 对适配的影响 |
|---|---|---|
| #20739 | merged | 增加 GLM-5.2 layerwise PD 的社区 E2E 证据；具体拓扑按现场 |
| #20940 | merged | 增加 Mooncake V2 layerwise push 能力 |
| #20625 | merged（部分发布线） | component-aware Mooncake layerwise transfer；main 分支对应能力仍需按 PR 确认 |
| #19361 | merged | PCP for Mooncake layerwise pooling |
| #20477 / #19639 | merged | MLA layerwise KV load waits |
| #20719 | merged | Mooncake layerwise 文档重写 |
| #20459 | merged | 移除 AscendStore layerwise 的 legacy lookup fallback |
| #21690 | merged 2026-11-16 | V2 入口支持 push connector |
| #21254 | merged 2026-11-16 | DCP 与 layerwise pooling 的组合能力 |
| #21437 | merged 2026-11-22 | hybrid/layerwise connector P 侧 prompt truncation 后重建 `block_hashes` |

## Open / 未合入

| PR/Issue | 状态 | 对适配的影响 |
|---|---|---|
| #21301 | open | 新 Store layerwise offload 契约，字段与旧 4-arena 不同 |
| #20152 / #21591 | open | component-aware transfer 在 main 的移植 |
| #20123 | open | DeepSeek V4-Flash layerwise P/D |
| #21364 | open | DeepSeek V4.1 layerwise KV save hooks |
| #21598 | open issue | MemCache layerwise 内存放大风险 |
| #13474 / #13825 | issue closed；fix PR open | AscendStore FirstWin 时 losing Mooncake 的 ownership / block 数不一致问题 |

## 已关闭 / 不采用

| PR/Issue | 状态 | 说明 |
|---|---|---|
| #21116 | closed 2026-11-26，未合入 | DeepSeek V4.1 cross-layer shared KV 不作为公开能力 |
| #21558 | closed issue | PUSH 完成时序问题；是否修复按版本确认 |

## 公开版字段说明

- 公开 vLLM-Ascend v0.30–v0.32/main 不把 `use_layerwise_range` / `layerwise_host_kv_offload` 作为标准 Mode A 字段。
- 公开 Store layerwise 相关字段以 `use_layerwise`、`layerwise_num_shared_buffers` 等官方文档为准。
- `use_ascend_direct` 属于 Mooncake V2 路径，不应写入 `MooncakeLayerwiseConnector` 子项。
- 旧 `patch_kv_cache_utils.py` 相关路径已从 main 删除，不再作为当前代码位置引用。

## 版本门禁

在 Mode Select 之前确认目标 checkout：

1. 是否包含公开版 P2P Layerwise 能力。
2. 是否包含 V2 push 能力。
3. 是否包含 component-aware 能力。
4. 是否包含新的官方 Store layerwise 契约。

缺失对应能力时按公开版基线处理，不要假设内部/fork 字段可用。
