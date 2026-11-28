#!/usr/bin/env bash
# 采集 P2P Layerwise 验证所需的只读证据。
#
# 用法：
#   P_LOG=/path/vllm-p.log D_LOG=/path/vllm-d.log \
#   CONFIG_JSON=/path/kv_transfer_config.json \
#   bash collect_layerwise_evidence.sh
#
# 可选环境变量：
#   OUT       输出文件，默认 layerwise_evidence.txt
#   FORCE=1   允许覆盖已有 OUT；默认拒绝覆盖
#   PYTHON_BIN Python 解释器，默认 python3
#
# 边界：只读取指定日志和配置，不修改配置、不重启服务、不扫描无关进程。
# 原始日志可能包含内部地址、端口或请求信息；对外分享前必须脱敏。
set -euo pipefail

umask 077

OUT="${OUT:-layerwise_evidence.txt}"
FORCE="${FORCE:-0}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
P_LOG="${P_LOG:-}"
D_LOG="${D_LOG:-}"
CONFIG_JSON="${CONFIG_JSON:-}"
LOG_PATTERN="MooncakeLayerwiseConnector|index_to_name|layer_metadata|num_kv_cache_groups|num_draft_attn_layers|runtime_has_indexer|MLAPO|layerwise|metaserver|WAITING_FOR_REMOTE_KVS"

RC=0

if [ -z "$OUT" ]; then
  echo "OUT must not be empty" >&2
  exit 2
fi
if [ -e "$OUT" ] && [ "$FORCE" != "1" ]; then
  echo "refusing to overwrite existing file: $OUT (set FORCE=1 to override)" >&2
  exit 2
fi
for path in "$P_LOG" "$D_LOG" "$CONFIG_JSON"; do
  [ -n "$path" ] || continue
  if [ -e "$OUT" ] && [ "$OUT" -ef "$path" ]; then
    echo "OUT must not be the same file as an input: $OUT" >&2
    exit 2
  fi
done

for path in "$P_LOG" "$D_LOG" "$CONFIG_JSON"; do
  if [ -n "$path" ] && [ ! -r "$path" ]; then
    echo "cannot read: $path" >&2
    RC=3
  fi
done

{
  echo "== evidence boundary =="
  echo "Only verification-related fields are collected."
  echo "Raw logs may contain internal addresses or request data; redact before sharing."
  echo
  echo "== date =="
  date -Is
  echo
  echo "== NPU =="
  npu-smi info 2>/dev/null || echo "npu-smi unavailable"
  echo
  echo "== versions =="
  "$PYTHON_BIN" -c "import sys, vllm, vllm_ascend; print(sys.version); print('vllm', getattr(vllm, '__version__', '?')); print('vllm_ascend', getattr(vllm_ascend, '__version__', '?'))" 2>/dev/null || true
  "$PYTHON_BIN" -c "import torch, torch_npu; print('torch', torch.__version__); print('torch_npu', getattr(torch_npu, '__version__', '?'))" 2>/dev/null || true
  echo
  if [ -n "$CONFIG_JSON" ] && [ -r "$CONFIG_JSON" ]; then
    echo "== KV transfer config =="
    cat "$CONFIG_JSON"
    echo
  fi
  for f in "$P_LOG" "$D_LOG"; do
    if [ -n "$f" ] && [ -r "$f" ]; then
      echo "== log: $f =="
      grep -E "$LOG_PATTERN" "$f" | tail -n 150 || true
      echo
    fi
  done
  cat <<'EOF'
== runtime values to capture (temporary debug print, not for production) ==
  print(list(kv_caches.keys()))
  print(len(kv_cache_config.kv_cache_groups))
  print([g.layer_names for g in kv_cache_config.kv_cache_groups])
  print(vllm_config.speculative_config)
  print(vllm_config.model_config.hf_text_config.index_topk)
  print(vllm_config.cache_config.block_size)
EOF
} | tee "$OUT"

echo "written: $OUT"
exit "$RC"
