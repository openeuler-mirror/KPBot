#!/bin/bash
# ============================================================================
# TP 闭环 Prefill 实例启动脚本模板（新方案：单机 TP + 显式 DP 组网 + 无 Ray）
# 由 kptools-tpsplit-optimization 生成脚本时替换所有 <占位符>。
# 每个实例一份独立脚本。实例间差异项（生成后 diff 应只出现这三处）：
#   --data-parallel-rank / ASCEND_RT_VISIBLE_DEVICES / --port
# 其余参数组内（或全局）一致，出现别的差异就是笔误。
# DP 组网参数为 vLLM 官方旗标：
#   --data-parallel-size <组大小>          一组 4 个 TP8 实例
#   --data-parallel-rank <rank>            本实例在组内的 rank（0/1/2/…）
#   --data-parallel-address <主节点IP>      组内统一
#   --data-parallel-rpc-port 16600         组内统一
# 注：--data-parallel-rank 外部 LB 模式适用于 MoE 模型（GLM 系列
# --enable-expert-parallel 场景即 MoE）；若现场 vLLM 版本旗标名不同，
# 以 `vllm serve --help | grep -i data-parallel` 实际输出为准。
# 注：本模板假设拓扑参数在脚本内定义（P_DP_SIZE/P_TP_SIZE 等本文件头部变量）。
# 若现场参数经外部 env 文件 source 传入，生成脚本应保持同样的传参形态
# （继续 source 同一文件，只改 env 文件里的值），见 SKILL.md 阶段 1.3。
# ============================================================================

# ===== 实例身份参数（每个实例不同）=====
export ASCEND_RT_VISIBLE_DEVICES=<DEVICES>       # 本实例绑定的卡，如 0,1,2,3,4,5,6,7；同机实例间禁止重叠

# ===== 拓扑参数（KV_TRANSFER_CONFIG 只改 P_DP_SIZE / P_TP_SIZE 两个值）=====
P_DP_SIZE=<P_DP_SIZE>     # Mooncake 全局 Prefill DP 副本总数 = 所有主机 P 实例数之和
P_TP_SIZE=<P_TP_SIZE>     # 本实例 TP 大小（如 8，单机闭环）
D_DP_SIZE=<D_DP_SIZE>     # D 侧现状（从旧脚本继承，不动）
D_TP_SIZE=<D_TP_SIZE>     # D 侧现状（从旧脚本继承，不动）

# ===== 本实例服务参数 =====
HTTP_PORT=<HTTP_PORT>                             # vLLM http 端口，同机双实例 9000/9001

# KV_TRANSFER_CONFIG：结构与旧脚本完全一致，只改 prefill 段的 dp_size/tp_size 值，
# engine_id / kv_port / lookup_rpc_port / decode 段一律原样保留，不要画蛇添足
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
"dp_size": '"$P_DP_SIZE"',
"tp_size": '"$P_TP_SIZE"'
},
"decode": {
"dp_size": '"$D_DP_SIZE"',
"tp_size": '"$D_TP_SIZE"'
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

# 无 --distributed-executor-backend ray：新方案 TP 单机闭环，显式 DP 组网，不依赖 Ray
vllm serve <MODEL_PATH> \
--host 0.0.0.0 \
--port "$HTTP_PORT" \
--block-size 128 \
--tensor-parallel-size "$P_TP_SIZE" \
--data-parallel-size <DP_GROUP_SIZE> \
--data-parallel-rank <RANK> \
--data-parallel-address <DP_MASTER_IP> \
--data-parallel-rpc-port 16600 \
--enable-expert-parallel \
--seed 1024 \
--no-async-scheduling \
--served-model-name <SERVED_MODEL_NAME> \
--max-model-len <MAX_MODEL_LEN> \
--max-num-batched-tokens <MAX_NUM_BATCHED_TOKENS> \
--max-num-seqs <MAX_NUM_SEQS> \
--tool-call-parser <TOOL_CALL_PARSER> \
--reasoning-parser <REASONING_PARSER> \
  --enable-prompt-tokens-details \
  --kv-transfer-config "$KV_TRANSFER_CONFIG" \
  --profiler-config '{"profiler": "torch","torch_profiler_dir": "xx","torch_profiler_with_stack": false}'
