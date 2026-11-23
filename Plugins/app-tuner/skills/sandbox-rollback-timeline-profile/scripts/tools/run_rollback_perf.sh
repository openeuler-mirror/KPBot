#!/usr/bin/env bash
# run_rollback_perf.sh - 回滚基准执行器：逐轮驱动 bench_rollback_concurrency.py
#
# 输入（环境变量）:
#   WORKDIR      必填，结果目录（将创建 rollback-latency/ 与 evidence/）
#   TEMPLATE_ID  必填，沙箱模板 ID
#   API_URL      默认 http://127.0.0.1:3000
#   API_KEY      必填，CubeSandbox API 密钥
#   BENCH_SCRIPT 必填，bench_rollback_concurrency.py 路径
#   PYTHON_BIN   默认 python3（需已安装 cubesandbox SDK）
#   CASE_MATRIX  默认 "profile-rb-c1-n5 1 5 0"，每行: name concurrency rounds dirty_mb
#
# 说明:
#   bench_rollback_concurrency.py 每次调用内部先做 1 轮 warm-up，再做 N 轮测量。
#   为拿到逐轮数据，本脚本对每个 case 的每个测量轮单独调用一次 "-n 1 --no-header"，
#   解析其唯一输出行（concurrency rounds wall_avg wall_min wall_p95 wall_max per_avg），
#   由于 N=1，五个耗时字段相等，即该轮的 batch wall_ms；per_ms = wall_ms/concurrency。

set -uo pipefail

WORKDIR=${WORKDIR:?set WORKDIR to the rollback result directory}
TEMPLATE_ID=${TEMPLATE_ID:?set TEMPLATE_ID to the benchmark template ID}
API_URL=${API_URL:-http://127.0.0.1:3000}
API_KEY=${API_KEY:?set API_KEY to the CubeSandbox API key}
BENCH_SCRIPT=${BENCH_SCRIPT:?set BENCH_SCRIPT to bench_rollback_concurrency.py path}
PYTHON_BIN=${PYTHON_BIN:-python3}
CASE_MATRIX=${CASE_MATRIX:-$'profile-rb-c1-n5 1 5 0\nprofile-rb-c5-n5 5 5 0\nprofile-rb-c10-n5 10 5 0'}

LATENCY_DIR="$WORKDIR/rollback-latency"
EVIDENCE_DIR="$WORKDIR/evidence"
mkdir -p "$LATENCY_DIR" "$EVIDENCE_DIR"

log() {
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$WORKDIR/run.log"
}

cleanup_sandboxes() {
  local items count round
  for round in $(seq 1 10); do
    items=$(curl -fsS --max-time 15 -H "Authorization: Bearer $API_KEY" \
      "$API_URL/sandboxes" 2>/dev/null || printf '[]')
    count=$(printf '%s' "$items" | jq 'length' 2>/dev/null || printf '0')
    [[ "${count:-0}" == "0" ]] && return 0
    printf '%s' "$items" | jq -r '.[] | .sandboxID // .sandbox_id // .id' 2>/dev/null |
      xargs -r -P 20 -I{} curl -fsS --max-time 60 -o /dev/null \
        -X DELETE -H "Authorization: Bearer $API_KEY" \
        "$API_URL/sandboxes/{}" || true
    sleep 2
  done
}

stats() {
  # stdin: 每行一个数值; 输出 JSON 字符串 {min,avg,p50,p95,max}
  local values
  values=$(grep -E '^-?[0-9]+(\.[0-9]+)?$' || true)
  [ -n "$values" ] || { printf '{"min":null,"avg":null,"p50":null,"p95":null,"max":null}\n'; return; }
  local sorted
  sorted=$(printf '%s\n' "$values" | sort -n)
  local n avg min max
  n=$(printf '%s\n' "$sorted" | wc -l)
  avg=$(printf '%s\n' "$sorted" | awk '{s+=$1} END{printf "%.3f", s/NR}')
  min=$(printf '%s\n' "$sorted" | head -1)
  max=$(printf '%s\n' "$sorted" | tail -1)
  local p50 p95
  p50=$(printf '%s\n' "$sorted" | awk -v n="$n" 'NR==int(n*0.50+0.999999){print; exit}')
  p95=$(printf '%s\n' "$sorted" | awk -v n="$n" 'NR==int(n*0.95+0.999999){print; exit}')
  printf '{"min":%s,"avg":%s,"p50":%s,"p95":%s,"max":%s}\n' \
    "$min" "$avg" "${p50:-$min}" "${p95:-$max}" "$max"
}

run_one_round() {
  # $1=concurrency $2=dirty_mb; 输出: "wall_ms per_ms"
  local cc=$1 dirty=$2 row wall per
  row=$(CUBE_API_URL="$API_URL" CUBE_API_KEY="$API_KEY" CUBE_TEMPLATE_ID="$TEMPLATE_ID" \
    "${PYTHON_BIN}" "$BENCH_SCRIPT" -c "$cc" -n 1 -d "$dirty" --no-header 2>/dev/null | tail -n 1 || true)
  wall=$(printf '%s' "$row" | awk '{print $3}')
  per=$(printf '%s' "$row" | awk '{print $7}')
  if ! printf '%s' "$wall" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
    return 1
  fi
  [ -n "$per" ] || per=$wall
  printf '%s %s\n' "$wall" "$per"
}

FAILED=0
mapfile -t cases < <(printf '%s\n' "$CASE_MATRIX" | sed '/^[[:space:]]*$/d')
for case_line in "${cases[@]}"; do
  read -r name cc rounds dirty <<<"$case_line"
  cc=${cc:-1}; rounds=${rounds:-5}; dirty=${dirty:-0}
  log "ROLLBACK_BEGIN name=${name} concurrency=${cc} rounds=${rounds} dirty_mb=${dirty}"

  cleanup_sandboxes

  raw_file="$LATENCY_DIR/$name.json"
  : >"$raw_file"
  raw_json='{"case":"'"$name"'","concurrency":'"$cc"',"rounds":'"$rounds"',"dirty_mb":'"$dirty"',"raw":[]}'

  wall_values=""; per_values=""; seq=0
  for i in $(seq 1 "$rounds"); do
    wall=""; per=""
    if read -r wall per <<<"$(run_one_round "$cc" "$dirty")"; then
      seq=$((seq + 1))
      printf -v wall_values '%s\n%s' "$wall_values" "$wall"
      printf -v per_values '%s\n%s' "$per_values" "$per"
      raw_json=$(printf '%s' "$raw_json" | \
        jq --argjson seq "$seq" --argjson wall "$wall" --argjson per "$per" \
           '.raw += [{"seq":$seq,"wall_ms":$wall,"per_ms":$per}]')
    else
      log "  round $i failed for $name"
    fi
    sleep 1
  done

  successful=$seq
  errors=$((rounds - successful))
  total_time_s=$(printf '%s\n' "$wall_values" | grep -E '^[0-9]' | awk '{s+=$1} END{printf "%.3f", s/1000}')
  throughput_rps=$(printf '%s\n' "$total_time_s" | \
    awk -v ok="$successful" '{if ($1>0) printf "%.3f", ok/$1; else printf "0"}')
  wall_stats=$(printf '%s\n' "$wall_values" | stats)
  per_stats=$(printf '%s\n' "$per_values" | stats)
  bench_exit=0
  if (( errors > 0 )); then bench_exit=1; FAILED=$((FAILED + 1)); fi

  success_rate=$(awk -v ok="$successful" -v total="$rounds" \
    'BEGIN { printf "%.3f", (total>0 ? ok/total : 0) }')

  printf '%s\n' "$raw_json" >"$raw_file"
  printf '{"case":"%s","concurrency":%s,"rounds":%s,"dirty_mb":%s,"bench_exit":%s,' \
    "$name" "$cc" "$rounds" "$dirty" "$bench_exit" \
    >"$LATENCY_DIR/$name.summary.json"
  printf '"successful":%s,"errors":%s,"success_rate":%s,"total_time_s":%s,"throughput_rps":%s,' \
    "$successful" "$errors" "$success_rate" "$total_time_s" "$throughput_rps" \
    >>"$LATENCY_DIR/$name.summary.json"
  printf '"wall_ms":%s,"per_rollback_ms":%s}\n' "$wall_stats" "$per_stats" \
    >>"$LATENCY_DIR/$name.summary.json"
done

exit "$FAILED"
