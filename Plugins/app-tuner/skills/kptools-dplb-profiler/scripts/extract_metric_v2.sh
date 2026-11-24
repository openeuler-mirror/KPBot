#!/usr/bin/env bash
# extract_metric_v2.sh - vLLM DP single-pool load metrics collection (v2, 30 data-column spec)
# Usage: ./extract_metric_v2.sh <sampling duration (seconds)> <urls file>
#   urls file: one http://ip:port/metrics per line; lines starting with # and blank lines are ignored
# Environment variables: SAMPLE_INTERVAL (sampling interval seconds, default 10), CURL_TIMEOUT (single fetch timeout seconds, default 5)
# Output: dp_load_metrics_<timestamp>.csv (v2 spec, 32-column header + queue_fixed marker), missing items filled with -1
#   timestamp column is human-readable local time "YYYY-MM-DD HH:MM:SS" (report side also accepts epoch seconds)
#   extra trailing column queue_fixed=1 marks rows where negative queue_sum was corrected (known vLLM export bug)
#   curl uses --noproxy '*' to bypass http_proxy on cluster nodes
# Header verification: grep ttft_sum <CSV>
set -u

DURATION="${1:-}"
URLS_FILE="${2:-}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-10}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

if [ -z "$DURATION" ] || [ -z "$URLS_FILE" ] || [ ! -f "$URLS_FILE" ]; then
  echo "Usage: $0 <sampling duration (seconds)> <urls file>" >&2
  exit 1
fi
case "$DURATION" in (*[!0-9]*|'') echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1;; esac
if [ "$DURATION" -le 0 ]; then echo "Error: sampling duration must be a positive integer (seconds)" >&2; exit 1; fi

URLS=()
TAGS=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  line="$(echo "$line" | tr -d '[:space:]')"
  [ -z "$line" ] && continue
  # Security: url files are typically generated from ticket/chat/wiki text (not fully
  #   trusted) -- whitelist each line so curl only ever fetches http(s) URLs (a crafted
  #   line like file:///etc/passwd would otherwise be fetched and parsed into the CSV).
  if ! printf '%s' "$line" | grep -qE '^https?://[A-Za-z0-9._:/-]+$'; then
    echo "Error: invalid URL line in $URLS_FILE (expect http(s)://host[:port]/path; allowed chars: A-Za-z0-9 . _ : / -): $line" >&2
    exit 1
  fi
  URLS+=("$line")
  tag="${line#http://}"; tag="${tag#https://}"; tag="${tag%%/*}"
  TAGS+=("$tag")
done < "$URLS_FILE"
if [ "${#URLS[@]}" -eq 0 ]; then echo "Error: no valid URLs in urls file" >&2; exit 1; fi

TS="$(date +%Y%m%d%H%M%S)"
OUT="dp_load_metrics_${TS}.csv"
echo "timestamp,ip_port,num_requests_running,num_requests_waiting,num_requests_swapped,request_success_total,request_failure_total,num_preemptions_total,prompt_tokens_total,generation_tokens_total,request_validation_time_total,kv_cache_usage_perc,cpu_cache_usage_perc,prefix_cache_hit_rate,pending_prefix_cache_hit_rate,external_prefix_cache_hit_rate,process_start_time_seconds,ttft_avg,queue_avg,prefill_avg,tpot_avg,e2e_avg,ttft_sum,ttft_count,queue_sum,queue_count,prefill_sum,prefill_count,tpot_sum,tpot_count,e2e_sum,e2e_count,queue_fixed" > "$OUT"

# Parse a single /metrics response and output one CSV data row (no trailing newline)
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

START=$(date +%s)
ROUNDS=0
while :; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  [ "$ELAPSED" -ge "$DURATION" ] && break
  ROUND_TS="$(date '+%Y-%m-%d %H:%M:%S')"
  ROUNDS=$((ROUNDS + 1))
  for i in "${!URLS[@]}"; do
    BODY=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${URLS[$i]}" 2>/dev/null) || BODY=""
    if [ -n "$BODY" ]; then
      printf '%s' "$BODY" | parse_row "$ROUND_TS" "${TAGS[$i]}" >> "$OUT"
    else
      echo "${ROUND_TS},${TAGS[$i]},-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1" >> "$OUT"
    fi
  done
  echo "[round $ROUNDS] elapsed ${ELAPSED}s / ${DURATION}s, rows $(wc -l < "$OUT")"
  NOW=$(date +%s)
  LEFT=$((DURATION - (NOW - START)))
  [ "$LEFT" -le 0 ] && break
  SLEEP=$((LEFT < SAMPLE_INTERVAL ? LEFT : SAMPLE_INTERVAL))
  sleep "$SLEEP"
done

echo "Sampling duration has been reached ($(( $(date +%s) - START )) seconds), performing final counter snapshot..."
for i in "${!URLS[@]}"; do
  BODY=$(curl -sS --noproxy '*' --max-time "$CURL_TIMEOUT" "${URLS[$i]}" 2>/dev/null) || BODY=""
  if [ -n "$BODY" ]; then
    LAST=$(printf '%s' "$BODY" | parse_row "final" "${TAGS[$i]}")
    SUCC=$(echo "$LAST" | cut -d, -f6)
    PROMPT=$(echo "$LAST" | cut -d, -f9)
    GEN=$(echo "$LAST" | cut -d, -f10)
    echo "  [${TAGS[$i]}] request_success_total=$SUCC prompt_tokens_total=$PROMPT generation_tokens_total=$GEN"
  else
    echo "  [${TAGS[$i]}] final snapshot failed (unreachable)"
  fi
done
echo "Sampling complete: $ROUNDS rounds x ${#URLS[@]} nodes, output $OUT"
