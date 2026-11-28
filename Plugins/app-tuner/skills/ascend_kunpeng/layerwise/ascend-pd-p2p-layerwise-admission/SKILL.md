---
name: ascend-pd-p2p-layerwise-admission
description: 昇腾 vLLM-Ascend PD 分离启用公开版 P2P Layerwise（MooncakeLayerwiseConnector / MultiConnector）前的准入检查。判断模型与 KV cache 结构是否满足逐层传输契约，输出 go/no-go、Mode A 建议、blockers 和风险；覆盖已验证的 GLM-5.2/5.3 场景与未验证模型边界。当用户问“能不能开 P2P layerwise”“需要看哪些指标”“某个模型/checkout 是否支持”时使用。作为 ascend_kunpeng/layerwise 子域使用。
compatibility: Linux + Ascend NPU; requires vLLM / vLLM-Ascend runtime and read-only access to P/D logs.
---

# P2P Layerwise 准入检查

本 skill 只覆盖公开 vLLM-Ascend 的 **Mode A**：P2P Layerwise + Store 非 layerwise。Store layerwise range、host offload 和内部 fork 字段不在本 skill 的承诺范围内。

## 何时触发

- 用户准备在 vLLM-Ascend PD 分离中启用 P2P 逐层传输。
- 用户询问某个模型、KV cache 结构或 checkout 是否满足 layerwise 契约。
- 需要先给出 go/no-go 和风险，再决定是否进入配置或排障阶段。

## 不进入本 skill

- 已经有明确配置错误或启动异常 → 走 `ascend-pd-p2p-layerwise-troubleshooting`。
- 只需要生成或校验配置 → 走 `ascend-pd-p2p-layerwise-config`。
- 需要采集 P/D 两侧环境差异 → 走 `kptools-ascend-pd-env`。
- 需要分析 profiling trace → 走 `kptools-ascend-profiling-analysis`。
- 结论必须通过修改 connector/planner 才能成立 → 交给 `code-optimizer`，本 skill 不做源码承诺。

## 必读 Reference

- `references/gates.md`：准入阶段内部流程：版本门禁 → Evidence → Gate → Mode A 输出。
- `references/matrix.md`：模型/场景支持矩阵，只保留有证据的结论。
- `references/glm52-53.md`：已记录场景的约束与观察点，不包含环境专属数据。
- `references/unverified-models.md`：未验证模型清单和禁止承诺。

## Input Modes

- standalone：用户直接询问能否启用 layerwise。
- 主编排调用：向本 skill 提供模型、KV cache 结构、checkout/PR 信息和部署形态。

## 输入证据

先采集事实，不要先按模型名下结论：

- `kv_caches.keys()` 与每个物理 layer index 的 key 数。
- `len(kv_cache_config.kv_cache_groups)`、每个 group 的 `layer_names` 和 spec 类型。
- attention 后端（`mla_v1` / `sfa_v1` / `attention_v1` 等）是否具备逐层 wait/save hook。
- `num_draft_attn_layers`。
- Store 子连接器的 `backend`、`use_layerwise`、`load_async`。
- checkout 是否包含公开版 P2P Layerwise 能力；版本门禁以现场代码为准。

## 六个 Gate

| Gate | 检查 | 不通过时的处理 |
|---|---|---|
| G1 | attention 后端有逐层 `wait` / `save` hook | 不要开 P2P layerwise，或换支持的 attention 路径 |
| G2 | 每个 layer index 只有一个 `kv_caches` key，或 indexer 已合并/未创建 | 先解决同 index 多 cache key，否则 connector 会断言 |
| G3 | draft attention layer 数量满足 connector 的单 MTP 映射约束 | 多 draft 需要源码扩展，交 `code-optimizer` |
| G4 | checkout 包含公开版 P2P Layerwise 能力 | 按现场版本回退，或升级到包含对应能力的发布线 |
| G5 | Store 子项使用 `use_layerwise=false` | 改为 Store 非 layerwise；Store layerwise 不在本 skill 范围内 |
| G6 | 配置不含 `use_layerwise_range` / `layerwise_host_kv_offload`，`load_async` 是 JSON boolean | 删除内部/fork 字段或见 troubleshooting 的版本说明 |

Mode A 判断细节见 `references/gates.md`。

## 输出

```text
verdict: no-go | mode-a
blockers: [...]
risks: [...]
evidence: {...}
next_action: admission | config | verification | troubleshooting | code-optimizer
```

## 拒绝条件

- 用户要求仅凭模型名直接承诺“支持/不支持”。
- 用户要求把未验证模型写成已验证。
- 用户要求把 Store layerwise range、host offload 或内部 fork 字段当成公开版标准配置。
- 用户要求在本 skill 中修改 connector/planner 源码。

## 边界

本 skill 只做准入判断，不生成部署配置、不执行验证、不修改源码。准入通过后进入 config；出现异常进入 troubleshooting。
