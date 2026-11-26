#!/usr/bin/env bash
# PD P2P Layerwise 服务器侧验证脚本模板。
#
# 用法：
#   PROXY_HOST=127.0.0.1 PROXY_PORT=9000 MODEL_NAME=<model> \
#   P_LOG=/path/vllm-p.log D_LOG=/path/vllm-d.log \
#   bash verify_p2p_layerwise.sh
#
# 退出码：
#   0 健康检查、请求和关键日志检查通过
#   1 健康检查或请求失败
#   2 参数非法
#   3 证据不足，无法判定
set -euo pipefail

umask 077

PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-9000}"
MODEL_NAME="${MODEL_NAME:-glm}"
P_LOG="${P_LOG:-/var/log/vllm-p.log}"
D_LOG="${D_LOG:-/var/log/vllm-d.log}"
CURL_MAX_TIME="${CURL_MAX_TIME:-10}"

LOG_PATTERN="MooncakeLayerwiseConnector|index_to_name|layer_metadata|num_kv_cache_groups|num_draft_attn_layers|MLAPO|layerwise|metaserver|WAITING_FOR_REMOTE_KVS"

RC=0

if ! [[ "$PROXY_HOST" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
  echo "invalid PROXY_HOST: $PROXY_HOST" >&2
  exit 2
fi
if ! [[ "$PROXY_PORT" =~ ^[0-9]+$ ]] || (( PROXY_PORT < 1 || PROXY_PORT > 65535 )); then
  echo "invalid PROXY_PORT: $PROXY_PORT" >&2
  exit 2
fi
if ! [[ "$MODEL_NAME" =~ ^[A-Za-z0-9._:/-]+$ ]]; then
  echo "invalid MODEL_NAME: $MODEL_NAME" >&2
  exit 2
fi
if ! [[ "$CURL_MAX_TIME" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid CURL_MAX_TIME: $CURL_MAX_TIME" >&2
  exit 2
fi

echo "== 1. NPU 状态 =="
npu-smi info 2>/dev/null || echo "npu-smi unavailable"

echo "== 2. 版本 =="
python3 -c "import vllm, vllm_ascend; print('vllm', vllm.__version__); print('vllm_ascend', getattr(vllm_ascend, '__version__', 'unknown'))" 2>/dev/null || true

echo "== 3. 服务健康 =="
if ! curl -sS --fail --max-time "$CURL_MAX_TIME" \
  "http://${PROXY_HOST}:${PROXY_PORT}/v1/models"; then
  echo "health check failed" >&2
  RC=1
fi

echo "== 4. 唯一 prompt 请求（避免 prefix cache 掩盖问题） =="
PROMPT="P2P layerwise verification $(date +%s) - $(head -c 64 /dev/urandom | base64 | tr -d '\n')"
PAYLOAD=$(python3 - "$MODEL_NAME" "$PROMPT" <<'PY'
import json
import sys

print(json.dumps({
    "model": sys.argv[1],
    "prompt": sys.argv[2],
    "max_tokens": 32,
}))
PY
)
if ! curl -sS --fail --max-time "$CURL_MAX_TIME" \
  "http://${PROXY_HOST}:${PROXY_PORT}/v1/completions" \
  -H "Content-Type: application/json" \
  --data-binary "$PAYLOAD"; then
  echo "completion request failed" >&2
  RC=1
fi

echo "== 5. P/D 日志关键行 =="
P_MARKERS=0
D_MARKERS=0
for f in "$P_LOG" "$D_LOG"; do
  if [ -r "$f" ]; then
    echo "--- $f ---"
    matches=$(grep -Ec "$LOG_PATTERN" "$f" || true)
    if [ "$f" = "$P_LOG" ]; then
      P_MARKERS=$matches
    fi
    if [ "$f" = "$D_LOG" ]; then
      D_MARKERS=$matches
    fi
    grep -E "$LOG_PATTERN" "$f" | tail -n 80 || true
  else
    echo "log not readable: $f" >&2
    if [ "$RC" -eq 0 ]; then
      RC=3
    fi
  fi
done
if [ "$P_MARKERS" -eq 0 ] || [ "$D_MARKERS" -eq 0 ]; then
  echo "no key layerwise markers found in P/D logs" >&2
  if [ "$RC" -eq 0 ]; then
    RC=3
  fi
fi

echo "== 6. 建议手工确认 =="
cat <<'EOF'
1) Store 是否保持 use_layerwise=false。
2) P2P connector：index_to_name 是否每 index 只有一个 name，或 indexer 已合并。
3) num_draft_attn_layers 是否满足 connector 的单 MTP 映射约束。
4) 精度：同一 prompt 对比本地单机输出，不能只看请求成功。
5) 数据边界：原始日志可能包含内部地址或请求信息，对外分享前必须脱敏。
EOF

if [ "$RC" -ne 0 ]; then
  echo "verification failed: rc=$RC" >&2
  exit "$RC"
fi
echo "verification passed"
