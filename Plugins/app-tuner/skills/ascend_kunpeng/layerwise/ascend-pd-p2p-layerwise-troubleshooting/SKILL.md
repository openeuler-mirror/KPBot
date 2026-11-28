---
name: ascend-pd-p2p-layerwise-troubleshooting
description: 排查公开版昇腾 vLLM-Ascend PD 分离 P2P Layerwise Mode A 的启动失败、传输超时、block 映射异常、FirstWin 冲突和精度问题。覆盖 MultiConnector/MooncakeLayerwiseConnector 的公开行为、remote_block_ids 后缀偏移、请求级重算兜底和社区版本门禁；当用户贴出 layerwise 报错、IndexError、WAITING_FOR_REMOTE_KVS、输出异常或问“为什么没触发/为什么错块”时使用。作为 ascend_kunpeng/layerwise 子域使用。
compatibility: Linux + Ascend NPU; requires vLLM / vLLM-Ascend runtime and read-only access to P/D logs.
---

# P2P Layerwise 排障

本 skill 只覆盖公开版 **Mode A**：P2P Layerwise + Store 非 layerwise。Store layerwise range、host offload 和内部 fork 字段不在本 skill 范围内。

## 何时触发

- 启动阶段报配置错误。
- 请求超时、卡在等待远端 KV、P2P 未触发。
- `get_transfer_mappings`、`remote_block_ids`、`d_block_idx` 相关 `IndexError`。
- AscendStore 赢得 FirstWin 后出现重复接收、错块或重发。
- 请求成功但输出异常，怀疑 KV 不完整。

## 不进入本 skill

- 还没有准入结论 → 先走 `ascend-pd-p2p-layerwise-admission`。
- 只需要生成/校验配置 → 走 `ascend-pd-p2p-layerwise-config`。
- 需要系统性采集证据和精度验证 → 走 `ascend-pd-p2p-layerwise-verification`。
- 需要修改 connector/planner 源码 → 交 `code-optimizer`。

## 必读 Reference

- `references/troubleshooting.md`：行为级症状、原因和处理。
- `references/community-status.md`：社区 PR/issue 版本门禁，带核对日期。
- `references/risk-grading.md`：Mode A 风险分级和验证方法。

## 排障顺序

1. 确认配置属于公开 Mode A：Store `use_layerwise=false`，不含 `use_layerwise_range` / `layerwise_host_kv_offload`。
2. 确认准入事实：`num_kv_cache_groups`、`kv_caches.keys()`、draft layer 数、attention 后端。
3. 确认传输是否触发：proxy 是否 D-first layerwise、metaserver 是否收到回调、P 是否进入 send、D 是否进入 recv。
4. 确认 block 映射一致：本地/远端 block 数、`transed_idx`、`to_trans_idx`、`remote_block_ids` 是否后缀。
5. 确认失败处理：是否按 `kv_load_failure_policy` 处理、D 是否 invalid block、是否真正重算/失败。
6. 最后再判断是否需要源码修改。

## 常见问题

完整现象、原因和处理见 `references/troubleshooting.md`。本 skill 只保留排障顺序，避免 SKILL.md 与 reference 双维护。

## Block 映射与请求级重算

当 D 只返回缓存前缀之后的后缀 `remote_block_ids` 时，P 侧不能继续用绝对 `d_block_idx`：

- 前缀块数应按 D 侧每个 rank 的 `(pcp_rank, dcp_rank)` 统计；
- `d_block_idx` 在后缀列表场景要减去该 rank 的前缀块数；
- 远端 block 数不足或本地 block 数不足时，不要 `continue` 静默跳过；
- 应把该请求收敛为失败：P 侧发送 `FAILED_SENDING_MSG`，D 侧将该请求的 block 标记失效，并按 `kv_load_failure_policy` 处理；
- 如果当前 checkout 没有这层兜底，至少关闭 Decode prefix cache 或用唯一 prompt 规避，并记录为已知缺陷。

## 版本门禁

- 以现场 checkout 是否包含对应公开能力为准，不按模型名推断。
- 社区状态只作为版本判断线索，不作为环境验证结论。
- 内部/fork 字段不作为公开 Mode A 的排障依据。

## 输出

```text
root_cause: config | ownership | block-mapping | version-gate | source-limit | unknown
evidence: [...]
workaround: ...
next_action: config | verification | code-optimizer | upgrade
```

## 拒绝条件

- 用户要求用静默跳过代替失败/重算。
- 用户要求隐瞒 block 数不一致或精度异常。
- 用户要求把内部/fork 字段当作公开版能力。
- 用户要求在本 skill 中直接修改 connector/planner 源码。

## 边界

本 skill 只做诊断、收敛和 workaround 建议。结构性修复交 `code-optimizer`，并需单独验证。
