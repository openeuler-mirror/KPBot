---
name: ascend-pd-p2p-layerwise-config
description: 生成和校验公开版昇腾 vLLM-Ascend PD 分离 P2P Layerwise 的 Mode A MultiConnector 配置。覆盖 MooncakeLayerwiseConnector、AscendStoreConnector 非 layerwise、Store backend/load_async、端口与 engine_id 等配置项；当用户问“Mode A 配置怎么写”“这个 JSON 是否合法”“启动前静态检查”时使用。作为 ascend_kunpeng/layerwise 子域使用。
compatibility: Linux + Ascend NPU; requires vLLM / vLLM-Ascend runtime and read-only access to P/D logs.
---

# P2P Layerwise 配置

本 skill 只覆盖公开 vLLM-Ascend 的 **Mode A**：

```text
MultiConnector[
  MooncakeLayerwiseConnector,
  AscendStoreConnector(use_layerwise=false)
]
```

Store layerwise range、`layerwise_host_kv_offload` 和内部 fork 字段不在本 skill 的配置模板和校验范围内。

## 何时触发

- 已通过准入检查，需要生成 `--kv-transfer-config`。
- 用户给出 Mode A MultiConnector JSON，要求检查字段或启动前风险。
- 用户询问 Store 非 layerwise、backend、load_async、端口或 engine_id 的配置建议。

## 不进入本 skill

- 还没有判断模型/KV cache 是否能开 → 先走 `ascend-pd-p2p-layerwise-admission`。
- 启动后报错或请求异常 → 走 `ascend-pd-p2p-layerwise-troubleshooting`。
- 需要 Store layerwise、host offload 或内部 fork 字段 → 不在公开版范围内，交 `code-optimizer` 或单独版本契约。
- 需要修改 connector/planner 才能支持 → 交 `code-optimizer`。

## 必读 Reference

- `references/mode-a.md`：Mode A 字段、约束和回退。
- `scripts/check_layerwise_config.py`：静态检查脚本。
- `tests/test_check_layerwise_config.py`：脚本单测。

## 输入

- 角色：Prefill / Decode。
- P2P connector：`MooncakeLayerwiseConnector`。
- Store：`AscendStoreConnector`，`use_layerwise=false`。
- P/D 拓扑、端口分配、engine_id 命名规则。
- 现场 `num_kv_cache_groups`、`kv_caches.keys()`、`num_draft_attn_layers` 等事实。

## 配置模板

以下为结构模板，尖括号项按现场填写；不要直接复制固定拓扑。

```jsonc
{
  "kv_connector": "MultiConnector",
  "kv_role": "kv_producer",              // Decode 侧改为 kv_consumer
  "engine_id": "<engine-id>",
  "kv_port": "<unique-port>",
  "kv_load_failure_policy": "fail",      // hybrid 必须 fail；非 hybrid 才评估 recompute
  "kv_connector_extra_config": {
    "connectors": [
      {
        "kv_connector": "MooncakeLayerwiseConnector",
        "kv_role": "kv_producer",
        "kv_port": "<p2p-port>",
        "kv_connector_extra_config": {
          "prefill": { "dp_size": <prefill-dp>, "tp_size": <prefill-tp> },
          "decode":  { "dp_size": <decode-dp>,  "tp_size": <decode-tp> }
        }
      },
      {
        "kv_connector": "AscendStoreConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "backend": "<store-backend>",
          "load_async": false,
          "use_layerwise": false
        }
      }
    ]
  }
}
```

Decode 侧调整：

- `kv_role` 改为 `kv_consumer`；
- P2P 和 Store 子项的 `kv_port` 使用另一组不冲突端口；
- Store 子项按需设置 `consumer_is_to_load` / `consumer_is_to_put`，但保持 `use_layerwise=false`。

## 配置检查

1. `kv_connector=MultiConnector`，包含 `MooncakeLayerwiseConnector`。
2. Store 子项 `use_layerwise=false`；如果为 true，说明超出本 skill 的公开 Mode A 范围。
3. `load_async` 必须是 JSON boolean，不要写字符串 `"false"`。
4. `engine_id` / `kv_port` / `lookup_rpc_port` 每个 rank 唯一。
5. 不设置 `use_layerwise_range`、`layerwise_host_kv_offload`；这些字段不属于公开 v0.30–v0.32/main 的 Mode A 契约。
6. 运行：

```bash
python3 scripts/check_layerwise_config.py <kv-transfer-config.json>
```

## 输出

```text
mode: A
config_status: valid | invalid | needs-evidence
issues: [...]
risks: [...]
next_action: apply | fix-config | verification | troubleshooting
```

## 拒绝条件

- 用户要求生成固定机型/固定拓扑的“标准答案”，但未提供现场事实。
- 用户要求启用 Store layerwise range、host offload 或内部 fork 字段。
- 用户要求用字符串布尔值绕过校验。
- 用户要求在本 skill 中修改源码。

## 边界

本 skill 只生成和校验公开 Mode A 配置，不启动服务、不采集证据、不判断最终精度。配置通过后进入 verification。
