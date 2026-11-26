# lbprof_common.awk —— dplb-profiler 采集脚本公共 awk 库（唯一权威副本）。
# 被 extract_metric_v2.sh / extract_metric_single.sh / extract_metric_pd.sh 的
# parse_row 以 `awk -f "$LIBDIR/lbprof_common.awk" ...' 加载；CSV 列输出（END 块）
# 仍由各脚本自带（口径见 csv_format.md）。

function val(s) { return (s == "" ? -1 : s + 0) }
function acc(name, v) { if (v != "") seen[name] = 1; got[name] += v }
function pick(pri, alt) { return (seen[pri] ? got[pri] : (seen[alt] ? got[alt] : "")) }
function out2(pri, alt) { v = pick(pri, alt); printf "%s,", val(v) }
# 命中率优先 hits/queries counter 累计比（queries=0 时记 0），回退旧版 gauge
function hrate(h, q, g) {
  if (seen[q]) {
    if (got[q] + 0 > 0) { hv = seen[h] ? got[h] + 0 : 0; return hv / (got[q] + 0) }
    return 0
  }
  if (seen[g]) return got[g] + 0
  return ""
}

# 值 = 最后一个纯数值字段：NaN/Inf/缺值/含空格 label 的非数值尾段一律记缺失
#（无 acc 值为 ""，避免 awk 隐式转 0；与 extract_metric_prom.py 的 NaN 过滤口径一致；
#  带 OpenMetrics 时间戳时（name value ts），末尾字段均为数值时取倒数第二个）
function lbprof_value(   v) {
  v = ""
  if ($NF ~ /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/) {
    v = $NF
    if (NF >= 3 && $(NF-1) ~ /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/) v = $(NF-1)
  }
  return v
}

# /metrics 单行主解析块（唯一副本）：供 lbprof_parse_row_v2 与 legacy parse_row 共用。
# gsub 去回车、跳过注释行、剥 label 取指标名、按 lbprof_value 规则解析值并累加
function lbprof_main() {
  gsub(/\r/, "")
  if ($1 ~ /^#/) next
  name = $1; sub(/\{.*$/, "", name)
  acc(name, lbprof_value())
}

# 基础 15 个输出列（gauge/counter 快照 + 两个命中率 + process_start）——
# v2 口径（33 列）与 legacy 口径（22 列）共有；前两列（timestamp、ip_port）由调用方先输出
function lbprof_emit_base() {
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
}

# v2 口径的 hist 段（5 个派生 avg + 10 个 sum/count 原始列 + queue_fixed 标记）：
# sum/count 双检缺失、负 queue 按 sum 级修正后再求平均（口径见 csv_format.md）。
# 供 lbprof_common.sh 的 lbprof_parse_row_v2 调用。
function lbprof_emit_v2_hist(   hs, hc, qs, qc, ps, pc, tps, tpc, es, ec, qfix) {
  hs = pick("vllm:time_to_first_token_seconds_sum", "")
  hc = pick("vllm:time_to_first_token_seconds_count", "")
  printf "%s,", (hs != "" && hc != "" && hc + 0 > 0) ? hs / hc : -1
  qs = pick("vllm:request_queue_time_seconds_sum", "")
  qc = pick("vllm:request_queue_time_seconds_count", "")
  ps = pick("vllm:prefill_time_seconds_sum", "vllm:request_prefill_time_seconds_sum")
  pc = pick("vllm:prefill_time_seconds_count", "vllm:request_prefill_time_seconds_count")
  # negative queue 修正（已知 vLLM 导出 bug）：queue_sum<0 -> queue=0, prefill_sum+=queue_sum
  qfix = 0
  if (qs != "" && qs + 0 < 0) { qfix = 1; if (ps != "") ps = ps + qs; qs = 0 }
  printf "%s,", (qs != "" && qc != "" && qc + 0 > 0) ? qs / qc : -1
  printf "%s,", (ps != "" && pc != "" && pc + 0 > 0) ? ps / pc : -1
  tps = pick("vllm:request_time_per_output_token_seconds_sum", "vllm:time_per_output_token_seconds_sum")
  tpc = pick("vllm:request_time_per_output_token_seconds_count", "vllm:time_per_output_token_seconds_count")
  printf "%s,", (tps != "" && tpc != "" && tpc + 0 > 0) ? tps / tpc : -1
  es = pick("vllm:e2e_request_latency_seconds_sum", "vllm:request_latency_seconds_sum")
  ec = pick("vllm:e2e_request_latency_seconds_count", "vllm:request_latency_seconds_count")
  printf "%s", (es != "" && ec != "" && ec + 0 > 0) ? es / ec : -1
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
}

# legacy 口径（22 列）的 hist 尾段：5 个派生 *_avg 列（无 sum/count 原始列）。
# 负 queue 按 sum 级修正后再求平均（(20-6)/2=7，而非 avg 级相加的 8）；字段缺失
#（不存在）与有效负值以 qs 是否存在区分，不用 -1 哨兵值判定（口径见 csv_format.md）。
# 供 lbprof_common.sh 的 lbprof_parse_row_legacy 调用。
function lbprof_emit_legacy_hist(   hs, hc, qs, qc, ps, pc, tps, tpc, es, ec) {
  hs = pick("vllm:time_to_first_token_seconds_sum", "")
  hc = pick("vllm:time_to_first_token_seconds_count", "")
  printf "%s,", (hs != "" && hc != "" && hc + 0 > 0) ? hs / hc : -1
  qs = pick("vllm:request_queue_time_seconds_sum", "")
  qc = pick("vllm:request_queue_time_seconds_count", "")
  ps = pick("vllm:prefill_time_seconds_sum", "vllm:request_prefill_time_seconds_sum")
  pc = pick("vllm:prefill_time_seconds_count", "vllm:request_prefill_time_seconds_count")
  if (qs != "" && qs + 0 < 0) { if (ps != "") ps = ps + qs; qs = 0 }
  printf "%s,", (qs != "" && qc != "" && qc + 0 > 0) ? qs / qc : -1
  printf "%s,", (ps != "" && pc != "" && pc + 0 > 0) ? ps / pc : -1
  tps = pick("vllm:request_time_per_output_token_seconds_sum", "vllm:time_per_output_token_seconds_sum")
  tpc = pick("vllm:request_time_per_output_token_seconds_count", "vllm:time_per_output_token_seconds_count")
  printf "%s,", (tps != "" && tpc != "" && tpc + 0 > 0) ? tps / tpc : -1
  es = pick("vllm:e2e_request_latency_seconds_sum", "vllm:request_latency_seconds_sum")
  ec = pick("vllm:e2e_request_latency_seconds_count", "vllm:request_latency_seconds_count")
  printf "%s\n", (es != "" && ec != "" && ec + 0 > 0) ? es / ec : -1
}
