#!/bin/bash
# ============================================================================
# 旧方案基线：跨机 TP32 + Ray 托管（legacy，仅用于识别现有部署 / 对比差异，
# 不要直接用于新部署）。迁移到新方案时对照本文件识别需要变更的点：
#   1. --distributed-executor-backend ray        → 删除（去 Ray）
#   2. --tensor-parallel-size 32                 → 新 TP size（如 8，单机闭环）
#   3. 无 DP 组网参数                            → --data-parallel-size + rank + master IP + rpc-port 16600
#   4. ASCEND_RT_VISIBLE_DEVICES 16 卡全量       → 按实例切分（每实例 8 卡，互斥）
#   5. KV_TRANSFER_CONFIG prefill dp/tp          → 同步新拓扑（所有 P 侧 + D 侧），只改这两个值，其余字段不动
#   6. max-model-len / max-num-batched-tokens / max-num-seqs → 按 TP8 复核（算力/显存池变小）
# ============================================================================

# Prefill

p_dp_size=5
p_tp_size=32

# Decode

d_dp_size=64
d_tp_size=1

export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15

KV_TRANSFER_CONFIG='{
"kv_connector": "MultiConnector",
"kv_role": "kv_producer",
"kv_load_failure_policy": "fail",
"engine_id": "0",
"kv_connector_extra_config": {
"connectors": [
{
"kv_connector": "MooncakeConnectorV1",
"kv_role": "kv_producer",
"kv_port": "30000",
"kv_connector_extra_config": {
"prefill": {
"dp_size": '"$p_dp_size"',
"tp_size": '"$p_tp_size"'
},
"decode": {
"dp_size": '"$d_dp_size"',
"tp_size": '"$d_tp_size"'
}
}
},
{
"kv_connector": "AscendStoreConnector",
"kv_role": "kv_producer",
"kv_connector_extra_config": {
"lookup_rpc_port": "10001",
"backend": "mooncake",
"load_async": false
}
}
]
}
}'

vllm serve /data/models/GLM-5.2-W4A8/ \
--host 0.0.0.0 \
--port 9000 \
--block-size 128 \
--distributed-executor-backend ray \
--tensor-parallel-size "$p_tp_size" \
--enable-expert-parallel \
--seed 1024 \
--no-async-scheduling \
--served-model-name glm52 \
--max-model-len 204800 \
--max-num-batched-tokens 4096 \
--max-num-seqs 16 \
--tool-call-parser glm47 \
--reasoning-parser glm45 \
--enable-prompt-tokens-details \
--kv-transfer-config "$KV_TRANSFER_CONFIG" \
--profiler-config '{"profiler": "torch","torch_profiler_dir": "xx","torch_profiler_with_stack": false}'
