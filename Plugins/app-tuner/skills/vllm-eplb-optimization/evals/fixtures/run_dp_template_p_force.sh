#!/bin/bash
# P (prefill) node launch template -- DeepSeek-V4.1-Flash, vLLM 0.32
set -e

export HCCL_CONNECT_TIMEOUT=1200
export VLLM_USE_V2_MODEL_RUNNER=0
unset DYNAMIC_EPLB
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export MOONCAKE_CONFIG_PATH=/data/mooncake/mooncake.json

vllm serve /data/models/DeepSeek-V4.1-Flash \
    --served-model-name dsv41 \
    --trust-remote-code \
    --tensor-parallel-size 1 \
    --data-parallel-size 8 \
    --enable-expert-parallel \
    --kv-transfer-config '{
        "kv_connector":"MooncakeConnector",
        "kv_role":"kv_producer",
        "kv_connector_extra_config":{"local_ip":"192.0.2.10","local_port":12801}
    }' \
    --additional-config '{
        "enable_cpu_binding":false,
        "enable_fused_mc2":2,
        "enable_flashcomm1":false,
        "enable_shared_expert_dp":true,
        "enable_force_eplb":true
    }' \
    --max-model-len 131072 \
    --max-num-seqs 32 \
    --port 8000
