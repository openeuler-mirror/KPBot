#!/usr/bin/env bash
# extract_metric_single.sh - vLLM DP load metrics collection (original spec, 20 data columns, historical reference only)
# Difference from v2: no histogram sum/count columns (ttft_sum, etc.), delay columns only have snapshot cumulative average *_avg
# Usage: ./extract_metric_single.sh <sampling duration (seconds)> <urls file>
#   urls file: one http://ip:port/metrics per line; lines starting with # and blank lines are ignored
# Environment variables: SAMPLE_INTERVAL (sampling interval seconds, default 10), CURL_TIMEOUT (single fetch timeout seconds, default 5)
# Output: dp_load_metrics_<timestamp>.csv (22-column header), missing items filled with -1
# Note: new deployments should use extract_metric_v2.sh (30 data-column v2 spec); this script is only for compatibility with historical data collection methods
# Depends on: lbprof_common.sh / lbprof_common.awk in the same directory (common library, must be uploaded together)
set -u

DURATION="${1:-}"
URLS_FILE="${2:-}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-10}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"

LIBDIR="${LBPROF_LIB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# 公共库（唯一实现，避免多脚本逐字复制）：URL 白名单、参数校验、行发射（含 legacy 22 列
# 的 lbprof_parse_row_legacy）、单池采样循环；legacy 特有差异仅剩输出文件表头
. "$LIBDIR/lbprof_common.sh"

lbprof_check_args_single "$DURATION" "$URLS_FILE" "Usage: $0 <sampling duration (seconds)> <urls file>"

lbprof_load_urls "$URLS_FILE"

TS="$(date +%Y%m%d%H%M%S)"
OUT="dp_load_metrics_${TS}.csv"
echo "timestamp,ip_port,num_requests_running,num_requests_waiting,num_requests_swapped,request_success_total,request_failure_total,num_preemptions_total,prompt_tokens_total,generation_tokens_total,request_validation_time_total,kv_cache_usage_perc,cpu_cache_usage_perc,prefix_cache_hit_rate,pending_prefix_cache_hit_rate,external_prefix_cache_hit_rate,process_start_time_seconds,ttft_avg,queue_avg,prefill_avg,tpot_avg,e2e_avg" > "$OUT"

# legacy 单池采集：22 列行发射（lbprof_parse_row_legacy）与采样循环均由公共库提供
lbprof_poll_loop "$DURATION" "$SAMPLE_INTERVAL" "$OUT" 22 lbprof_parse_row_legacy " (original 20 data-column spec; new deployments should use extract_metric_v2.sh)"
