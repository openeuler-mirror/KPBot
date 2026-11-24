#!/usr/bin/env bash
# extract_metric_pd.sh - vLLM PD split cluster dual-pool (P/D) simultaneous load metrics collection (v2, 30 data-column spec)
# One run simultaneously samples all nodes in both P and D pools in the same round, producing two v2-spec CSVs with aligned timestamps
# Usage: ./extract_metric_pd.sh <sampling duration (seconds)> <p_urls file> <d_urls file>
#   urls file: one http://ip:port/metrics per line; lines starting with # and blank lines are ignored; both url groups must be non-empty
# Environment variables: SAMPLE_INTERVAL (sampling interval seconds, default 10), CURL_TIMEOUT (single fetch timeout seconds, default 5)
# Output: dp_load_metrics_P_<timestamp>.csv and dp_load_metrics_D_<timestamp>.csv, missing items filled with -1
#   timestamp column is human-readable local time "YYYY-MM-DD HH:MM:SS" (report side also accepts epoch seconds)
#   extra trailing column queue_fixed=1 marks rows where negative queue_sum was corrected (known vLLM export bug)
#   curl uses --noproxy '*' to bypass http_proxy on cluster nodes
set -u

DURATION="${1:-}"
P_URLS_FILE="${2:-}"
D_URLS_FILE="${3:-}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-10}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

LIBDIR="${LBPROF_LIB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# 公共库（唯一实现，避免多脚本逐字复制）：URL 白名单、33 列行发射 lbprof_parse_row_v2；
# PD 特有的双池并行采样循环与分组加载保留在本脚本
. "$LIBDIR/lbprof_common.sh"

if [ -z "$DURATION" ] || [ -z "$P_URLS_FILE" ] || [ -z "$D_URLS_FILE" ] || [ ! -f "$P_URLS_FILE" ] || [ ! -f "$D_URLS_FILE" ]; then
  echo "Usage: $0 <sampling duration (seconds)> <p_urls file> <d_urls file>" >&2
  exit 1
fi
case "$DURATION" in (*[!0-9]*|'') echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1;; esac

load_urls() {
  # $1 = urls file, $2 = target array name prefix ("P" or "D")
  # 每行过 http(s) 白名单校验后经显式分支做直接数组赋值追加 -- 从不 eval。
  #（eval 会重新解析行内容，恶意节点列表里的引号/$()/反引号可逃出数组赋值在
  #   采集机上执行命令；安全规则与白名单唯一实现见 lbprof_common.sh。）
  local f="$1" prefix="$2"; local line tag
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    lbprof_check_url "$line" "$f"
    if [ "$prefix" = "P" ]; then
      P_URLS_ALL+=("$line")
      P_TAGS_ALL+=("$(lbprof_url_tag "$line")")
    else
      D_URLS_ALL+=("$line")
      D_TAGS_ALL+=("$(lbprof_url_tag "$line")")
    fi
  done < "$f"
}

P_URLS_ALL=(); P_TAGS_ALL=()
load_urls "$P_URLS_FILE" "P"
P_COUNT=${#P_URLS_ALL[@]}
D_URLS_ALL=(); D_TAGS_ALL=()
load_urls "$D_URLS_FILE" "D"
D_COUNT=${#D_URLS_ALL[@]}

if [ "$P_COUNT" -eq 0 ] || [ "$D_COUNT" -eq 0 ]; then
  echo "Error: both P and D url groups must be non-empty (P=$P_COUNT, D=$D_COUNT)" >&2
  exit 1
fi

TS="$(date +%Y%m%d%H%M%S)"
OUT_P="dp_load_metrics_P_${TS}.csv"
OUT_D="dp_load_metrics_D_${TS}.csv"
HEADER="timestamp,ip_port,num_requests_running,num_requests_waiting,num_requests_swapped,request_success_total,request_failure_total,num_preemptions_total,prompt_tokens_total,generation_tokens_total,request_validation_time_total,kv_cache_usage_perc,cpu_cache_usage_perc,prefix_cache_hit_rate,pending_prefix_cache_hit_rate,external_prefix_cache_hit_rate,process_start_time_seconds,ttft_avg,queue_avg,prefill_avg,tpot_avg,e2e_avg,ttft_sum,ttft_count,queue_sum,queue_count,prefill_sum,prefill_count,tpot_sum,tpot_count,e2e_sum,e2e_count,queue_fixed"
echo "$HEADER" > "$OUT_P"
echo "$HEADER" > "$OUT_D"

TMPDIR_LB="$(mktemp -d /tmp/lbprof_pd.XXXXXX 2>/dev/null || mktemp -d)"
trap 'rm -rf "$TMPDIR_LB"' EXIT

fetch_one() {
  # 单行解析（33 列 v2 口径）由公共库 lbprof_parse_row_v2 提供（唯一副本）
  local url="$1" tag="$2" ts="$3" out="$4"
  local body
  body=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "$url" 2>/dev/null) || body=""
  if [ -n "$body" ]; then
    printf '%s' "$body" | lbprof_parse_row_v2 "$ts" "$tag" > "$out"
  else
    echo "${ts},${tag},-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1" > "$out"
  fi
}

START=$(date +%s)
ROUNDS=0
while :; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  [ "$ELAPSED" -ge "$DURATION" ] && break
  ROUND_TS="$(date '+%Y-%m-%d %H:%M:%S')"
  ROUNDS=$((ROUNDS + 1))
  for i in "${!P_URLS_ALL[@]}"; do
    fetch_one "${P_URLS_ALL[$i]}" "${P_TAGS_ALL[$i]}" "$ROUND_TS" "$TMPDIR_LB/p_$i.row" &
  done
  for i in "${!D_URLS_ALL[@]}"; do
    fetch_one "${D_URLS_ALL[$i]}" "${D_TAGS_ALL[$i]}" "$ROUND_TS" "$TMPDIR_LB/d_$i.row" &
  done
  wait
  for i in "${!P_URLS_ALL[@]}"; do cat "$TMPDIR_LB/p_$i.row" >> "$OUT_P"; done
  for i in "${!D_URLS_ALL[@]}"; do cat "$TMPDIR_LB/d_$i.row" >> "$OUT_D"; done
  echo "[round $ROUNDS] elapsed ${ELAPSED}s / ${DURATION}s, P rows $(wc -l < "$OUT_P"), D rows $(wc -l < "$OUT_D")"
  NOW=$(date +%s)
  LEFT=$((DURATION - (NOW - START)))
  [ "$LEFT" -le 0 ] && break
  SLEEP=$((LEFT < SAMPLE_INTERVAL ? LEFT : SAMPLE_INTERVAL))
  sleep "$SLEEP"
done

echo "Sampling duration has been reached ($(( $(date +%s) - START )) seconds), performing final counter snapshot..."
for i in "${!P_URLS_ALL[@]}"; do
  BODY=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${P_URLS_ALL[$i]}" 2>/dev/null) || BODY=""
  if [ -n "$BODY" ]; then
    LAST=$(printf '%s' "$BODY" | lbprof_parse_row_v2 "final" "${P_TAGS_ALL[$i]}")
    echo "  [P/${P_TAGS_ALL[$i]}] request_success_total=$(echo "$LAST" | cut -d, -f6) prompt_tokens_total=$(echo "$LAST" | cut -d, -f9) generation_tokens_total=$(echo "$LAST" | cut -d, -f10)"
  else
    echo "  [P/${P_TAGS_ALL[$i]}] final snapshot failed (unreachable)"
  fi
done
for i in "${!D_URLS_ALL[@]}"; do
  BODY=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${D_URLS_ALL[$i]}" 2>/dev/null) || BODY=""
  if [ -n "$BODY" ]; then
    LAST=$(printf '%s' "$BODY" | lbprof_parse_row_v2 "final" "${D_TAGS_ALL[$i]}")
    echo "  [D/${D_TAGS_ALL[$i]}] request_success_total=$(echo "$LAST" | cut -d, -f6) prompt_tokens_total=$(echo "$LAST" | cut -d, -f9) generation_tokens_total=$(echo "$LAST" | cut -d, -f10)"
  else
    echo "  [D/${D_TAGS_ALL[$i]}] final snapshot failed (unreachable)"
  fi
done
echo "Sampling complete: $ROUNDS rounds, P pool ${P_COUNT} nodes -> $OUT_P, D pool ${D_COUNT} nodes -> $OUT_D"
