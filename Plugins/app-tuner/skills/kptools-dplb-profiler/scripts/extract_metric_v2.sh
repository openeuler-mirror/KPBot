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
# Depends on: lbprof_common.sh / lbprof_common.awk in the same directory (common library, must be uploaded together)
set -u

DURATION="${1:-}"
URLS_FILE="${2:-}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-10}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

LIBDIR="${LBPROF_LIB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# 公共库（唯一实现，避免多脚本逐字复制）：URL 白名单、33 列行发射 lbprof_parse_row_v2、
# 单池采样循环 lbprof_poll_loop；内部自动加载 awk 公共函数库到 LBPROF_AWK_LIB
. "$LIBDIR/lbprof_common.sh"

lbprof_check_args_single "$DURATION" "$URLS_FILE" "Usage: $0 <sampling duration (seconds)> <urls file>"

lbprof_load_urls "$URLS_FILE"

TS="$(date +%Y%m%d%H%M%S)"
OUT="dp_load_metrics_${TS}.csv"
echo "timestamp,ip_port,num_requests_running,num_requests_waiting,num_requests_swapped,request_success_total,request_failure_total,num_preemptions_total,prompt_tokens_total,generation_tokens_total,request_validation_time_total,kv_cache_usage_perc,cpu_cache_usage_perc,prefix_cache_hit_rate,pending_prefix_cache_hit_rate,external_prefix_cache_hit_rate,process_start_time_seconds,ttft_avg,queue_avg,prefill_avg,tpot_avg,e2e_avg,ttft_sum,ttft_count,queue_sum,queue_count,prefill_sum,prefill_count,tpot_sum,tpot_count,e2e_sum,e2e_count,queue_fixed" > "$OUT"

# v2 单池采集：33 列行发射与采样循环均由公共库提供（本脚本只负责参数校验与文件命名）
lbprof_poll_loop "$DURATION" "$SAMPLE_INTERVAL" "$OUT" 33 lbprof_parse_row_v2 ""
