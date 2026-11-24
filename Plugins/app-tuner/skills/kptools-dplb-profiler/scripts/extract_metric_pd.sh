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

if [ -z "$DURATION" ] || [ -z "$P_URLS_FILE" ] || [ -z "$D_URLS_FILE" ] || [ ! -f "$P_URLS_FILE" ] || [ ! -f "$D_URLS_FILE" ]; then
  echo "Usage: $0 <sampling duration (seconds)> <p_urls file> <d_urls file>" >&2
  exit 1
fi
case "$DURATION" in (*[!0-9]*|'') echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1;; esac

load_urls() {
  # $1 = urls file, $2 = target array name prefix ("P" or "D")
  # Security: url files are typically generated from ticket/chat/wiki text and are
  #   NOT fully trusted. Each line must pass a strict whitelist (http(s):// plus a
  #   URL-safe charset) and is appended via direct array assignment in explicit
  #   branches -- never eval. (eval re-parses the line, so quotes/$()/backticks in
  #   a malicious node list could escape the array assignment and execute commands
  #   on the collector host.)
  local f="$1" prefix="$2"; local line tag
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -z "$line" ] && continue
    if ! printf '%s' "$line" | grep -qE '^https?://[A-Za-z0-9._:/-]+$'; then
      echo "Error: invalid URL line in $f (expect http(s)://host[:port]/path; allowed chars: A-Za-z0-9 . _ : / -): $line" >&2
      exit 1
    fi
    if [ "$prefix" = "P" ]; then
      P_URLS_ALL+=("$line")
      tag="${line#http://}"; tag="${tag#https://}"; tag="${tag%%/*}"
      P_TAGS_ALL+=("$tag")
    else
      D_URLS_ALL+=("$line")
      tag="${line#http://}"; tag="${tag#https://}"; tag="${tag%%/*}"
      D_TAGS_ALL+=("$tag")
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

parse_row() {
  awk -v ts="$1" -v tag="$2" '
    function val(s) { return (s == "" ? -1 : s + 0) }
    function acc(name, v) { if (v != "") seen[name] = 1; got[name] += v }
    function pick(pri, alt) { return (seen[pri] ? got[pri] : (seen[alt] ? got[alt] : "")) }
    function out2(pri, alt) { v = pick(pri, alt); printf "%s,", val(v) }
    # 命中率：优先 hits/queries counter 累计比（queries=0 时记 0），回退旧版 gauge
    function hrate(h, q, g) {
      if (seen[q]) {
        if (got[q] + 0 > 0) { hv = seen[h] ? got[h] + 0 : 0; return hv / (got[q] + 0) }
        return 0
      }
      if (seen[g]) return got[g] + 0
      return ""
    }
    {
      gsub(/\r/, "")
      if ($1 ~ /^#/) next
      name = $1; sub(/\{.*$/, "", name)
      # 值 = 最后一个纯数值字段：NaN/Inf/缺值/含空格 label 的非数值尾段一律记缺失
      # （acc 收 ""，不被 awk 隐式转 0——与 extract_metric_prom.py 的 NaN 过滤口径一致）；
      # 带 OpenMetrics 可选时间戳（name value ts，末两字段均为数值）时取倒数第二个
      v = ""
      if ($NF ~ /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/) {
        v = $NF
        if (NF >= 3 && $(NF-1) ~ /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/) v = $(NF-1)
      }
      acc(name, v)
    }
    END {
      printf "%s,%s,", ts, tag
      out2("vllm:num_requests_running", "")
      out2("vllm:num_requests_waiting", "")
      out2("vllm:num_requests_swapped", "")
      out2("vllm:request_success_total", "")
      out2("vllm:request_failure_total", "")
      out2("vllm:num_preemptions_total", "")
      out2("vllm:prompt_tokens_total", "")
      out2("vllm:generation_tokens_total", "")
      out2("vllm:request_validation_time_total", "")
      out2("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc")
      out2("vllm:cpu_cache_usage_perc", "")
      printf "%s,", val(hrate("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:prefix_cache_hit_rate"))
      out2("vllm:pending_prefix_cache_hit_rate", "")
      printf "%s,", val(hrate("vllm:external_prefix_cache_hits_total", "vllm:external_prefix_cache_queries_total", "vllm:external_prefix_cache_hit_rate"))
      out2("process_start_time_seconds", "")
      hs = pick("vllm:time_to_first_token_seconds_sum", "")
      hc = pick("vllm:time_to_first_token_seconds_count", "")
      printf "%s,", (hc != "" && hc + 0 > 0) ? hs / hc : -1
      qs = pick("vllm:request_queue_time_seconds_sum", "")
      qc = pick("vllm:request_queue_time_seconds_count", "")
      ps = pick("vllm:prefill_time_seconds_sum", "vllm:request_prefill_time_seconds_sum")
      pc = pick("vllm:prefill_time_seconds_count", "vllm:request_prefill_time_seconds_count")
      # negative queue 修正（已知 vLLM 导出 bug）：queue_sum<0 -> queue=0, prefill_sum+=queue_sum
      qfix = 0
      if (qs != "" && qs + 0 < 0) { qfix = 1; if (ps != "") ps = ps + qs; qs = 0 }
      printf "%s,", (qc != "" && qc + 0 > 0) ? qs / qc : -1
      printf "%s,", (pc != "" && pc + 0 > 0) ? ps / pc : -1
      tps = pick("vllm:request_time_per_output_token_seconds_sum", "vllm:time_per_output_token_seconds_sum")
      tpc = pick("vllm:request_time_per_output_token_seconds_count", "vllm:time_per_output_token_seconds_count")
      printf "%s,", (tpc != "" && tpc + 0 > 0) ? tps / tpc : -1
      es = pick("vllm:e2e_request_latency_seconds_sum", "vllm:request_latency_seconds_sum")
      ec = pick("vllm:e2e_request_latency_seconds_count", "vllm:request_latency_seconds_count")
      printf "%s", (ec != "" && ec + 0 > 0) ? es / ec : -1
      printf ","
      out2("vllm:time_to_first_token_seconds_sum", "")
      out2("vllm:time_to_first_token_seconds_count", "")
      printf "%s,", val(qs)
      printf "%s,", val(qc)
      printf "%s,", val(ps)
      printf "%s,", val(pc)
      out2("vllm:request_time_per_output_token_seconds_sum", "vllm:time_per_output_token_seconds_sum")
      out2("vllm:request_time_per_output_token_seconds_count", "vllm:time_per_output_token_seconds_count")
      out2("vllm:e2e_request_latency_seconds_sum", "vllm:request_latency_seconds_sum")
      printf "%s,%s\n", val(pick("vllm:e2e_request_latency_seconds_count", "vllm:request_latency_seconds_count")), qfix
    }'
}

TMPDIR_LB="$(mktemp -d /tmp/lbprof_pd.XXXXXX 2>/dev/null || mktemp -d)"
trap 'rm -rf "$TMPDIR_LB"' EXIT

fetch_one() {
  local url="$1" tag="$2" ts="$3" out="$4"
  local body
  body=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "$url" 2>/dev/null) || body=""
  if [ -n "$body" ]; then
    printf '%s' "$body" | parse_row "$ts" "$tag" > "$out"
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
    LAST=$(printf '%s' "$BODY" | parse_row "final" "${P_TAGS_ALL[$i]}")
    echo "  [P/${P_TAGS_ALL[$i]}] request_success_total=$(echo "$LAST" | cut -d, -f6) prompt_tokens_total=$(echo "$LAST" | cut -d, -f9) generation_tokens_total=$(echo "$LAST" | cut -d, -f10)"
  else
    echo "  [P/${P_TAGS_ALL[$i]}] final snapshot failed (unreachable)"
  fi
done
for i in "${!D_URLS_ALL[@]}"; do
  BODY=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${D_URLS_ALL[$i]}" 2>/dev/null) || BODY=""
  if [ -n "$BODY" ]; then
    LAST=$(printf '%s' "$BODY" | parse_row "final" "${D_TAGS_ALL[$i]}")
    echo "  [D/${D_TAGS_ALL[$i]}] request_success_total=$(echo "$LAST" | cut -d, -f6) prompt_tokens_total=$(echo "$LAST" | cut -d, -f9) generation_tokens_total=$(echo "$LAST" | cut -d, -f10)"
  else
    echo "  [D/${D_TAGS_ALL[$i]}] final snapshot failed (unreachable)"
  fi
done
echo "Sampling complete: $ROUNDS rounds, P pool ${P_COUNT} nodes -> $OUT_P, D pool ${D_COUNT} nodes -> $OUT_D"
