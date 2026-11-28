# 公开版 Mode A 准入流程

本文件只覆盖公开 vLLM-Ascend 的 Mode A，不包含 Store layerwise range、host offload 或内部 fork 字段。端到端编排由 `ascend-pd-p2p-layerwise-workflow` 负责；版本门禁在本阶段执行一次。

## 0. 版本门禁

确认目标 checkout 是公开 vLLM-Ascend 发布线，且包含 `MooncakeLayerwiseConnector` 的 P2P 逐层传输能力。以现场代码、启动日志和发布线为准，不按模型名推断。

公开 v0.30–v0.32/main 不把 `use_layerwise_range` / `layerwise_host_kv_offload` 作为标准 Mode A 字段；如果配置中出现这些字段，先确认是否为内部 fork 契约，不要当作已开启。

## 1. Evidence

采集：

- `kv_caches.keys()`
- `len(kv_cache_config.kv_cache_groups)` 及每个 group 的 `layer_names` / spec 类型
- `num_draft_attn_layers`
- `attn_backend`
- `runtime_has_indexer`
- Store 的 `backend` / `load_async` / `use_layerwise`
- P/D 拓扑

## 2. Gate

- G1：attention 后端有逐层 wait/save hook。
- G2：每个物理 layer index 只有一个 `kv_caches` key，或 indexer 已合并/未创建。
- G3：draft attention layer 数量满足 connector 的单 MTP 映射约束。
- G4：checkout 包含公开版 P2P Layerwise 能力。
- G5：Store 子项 `use_layerwise=false`。
- G6：配置不含 `use_layerwise_range` / `layerwise_host_kv_offload`，`load_async` 为 JSON boolean。

## 3. Mode Select

- P2P G1-G4 均通过、Store 满足 G5/G6 → Mode A。
- 任一 P2P gate 失败 → no-go，或交 `code-optimizer` 评估源码改动。
- Store layerwise range / host offload / 内部 fork 字段 → 超出本 skill 范围，停止并另开专题。

## 4. 输出

```json
{
  "stage": "gate",
  "verdict": "no-go|mode-a",
  "evidence": {},
  "blockers": [],
  "risks": [],
  "next_action": ""
}
```
