# Mode A 配置约束

Mode A 是公开 vLLM-Ascend 版 P2P Layerwise 的唯一推荐形态：

```text
MultiConnector[
  MooncakeLayerwiseConnector,
  AscendStoreConnector(use_layerwise=false)
]
```

完整 JSON 模板和字段填写方式以 `SKILL.md` 的「配置模板」为准；本文件只保留约束、失败策略和回退顺序，避免模板双维护。

## 约束

- P2P connector：`MooncakeLayerwiseConnector`。
- Store connector：`AscendStoreConnector`，`use_layerwise=false`。
- `load_async`：JSON boolean。
- 每个 rank 的 `engine_id` / `kv_port` / `lookup_rpc_port` 唯一。
- proxy：D-first layerwise proxy。
- `kv_load_failure_policy`：默认 `fail`；只有确认非 hybrid 场景时才评估 `recompute`。

## 不属于公开 Mode A 的字段

以下字段在公开 vLLM-Ascend v0.30–v0.32/main 中不作为标准 Mode A 配置：

- `use_layerwise_range`
- `layerwise_host_kv_offload`
- `use_ascend_direct`（如果需要 Mooncake V2 路径，按对应版本的 V2 文档单独配置，不要放进 `MooncakeLayerwiseConnector` 子项）

遇到这些字段时，不要按“已开启”继续验证；先确认目标 checkout 是否有对应实现，或使用公开版支持的替代路径。

## 校验与回退

- 运行 `python3 scripts/check_layerwise_config.py <config.json>`。
- 启动后确认 `num_kv_cache_groups`、`kv_caches.keys()`、`num_draft_attn_layers` 与准入结论一致。
- 用唯一 prompt 做精度对比，不能只看请求成功。
- 回退时恢复原配置，并按 Mooncake Master → Decode → Prefill → Proxy 的顺序重启。
