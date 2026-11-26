#!/usr/bin/env bash
# run_rollback_perf.sh - 回滚基准执行器：逐轮驱动 bench_rollback_concurrency.py
#
# 输入（环境变量）:
#   WORKDIR      必填，结果目录（将创建 rollback-latency/ 与 evidence/）
#   TEMPLATE_ID  必填，沙箱模板 ID
#   API_URL      默认 http://127.0.0.1:3000
#   API_KEY      必填，CubeSandbox API 密钥（只经环境变量与 0600 临时 curl
#                配置文件传递，不进入进程 argv）
#   BENCH_SCRIPT 必填，bench_rollback_concurrency.py 路径
#   PYTHON_BIN   默认 python3（需已安装 cubesandbox SDK）
#   CASE_MATRIX  默认三档用例，每行: name concurrency rounds dirty_mb
#   ALLOW_GLOBAL_CLEANUP  默认 0。=1 时额外删除 API 密钥可见的全部遗留沙箱
#                （仅独占基准机时使用）
#
# 说明:
#   bench_rollback_concurrency.py 每次调用内部先做 1 轮 warm-up，再做 N 轮测量。
#   为拿到逐轮数据，本脚本对每个 case 的每个测量轮单独调用一次 "-n 1"，
#   从 stdout 取最后一条可解析的数据行（concurrency rounds wall_avg wall_min
#   wall_p95 wall_max per_avg，wall=$3、per=$7），由于 N=1，五个耗时字段相等，
#   即该轮的 batch wall_ms；per_ms = wall_ms/concurrency。
#   bench 非零退出或找不到可解析数据行即判定该轮失败：不计入成功轮、不写 raw、
#   计入 errors 并使 bench_exit 非零，失败 case 数最终经退出码传回调用方。
#   沙箱清理只针对本次基准新建的沙箱（与 case 开始时的基线集合做差）；既有
#   遗留沙箱默认仅告警不删除，除非 ALLOW_GLOBAL_CLEANUP=1。
#   每组用例向 run.log 写 ROLLBACK_BEGIN/ROLLBACK_END 时间窗口标记，供
#   analyze_rollback_profile.mjs 按窗口把日志关联到对应 case。

set -uo pipefail

WORKDIR=${WORKDIR:?set WORKDIR to the rollback result directory}
TEMPLATE_ID=${TEMPLATE_ID:?set TEMPLATE_ID to the benchmark template ID}
API_URL=${API_URL:-http://127.0.0.1:3000}
API_KEY=${API_KEY:?set API_KEY to the CubeSandbox API key}
BENCH_SCRIPT=${BENCH_SCRIPT:?set BENCH_SCRIPT to bench_rollback_concurrency.py path}
PYTHON_BIN=${PYTHON_BIN:-python3}
CASE_MATRIX=${CASE_MATRIX:-$'profile-rb-c1-n5 1 5 0\nprofile-rb-c5-n5 5 5 0\nprofile-rb-c10-n5 10 5 0'}
ALLOW_GLOBAL_CLEANUP=${ALLOW_GLOBAL_CLEANUP:-0}

LATENCY_DIR="$WORKDIR/rollback-latency"
EVIDENCE_DIR="$WORKDIR/evidence"
mkdir -p "$LATENCY_DIR" "$EVIDENCE_DIR"

# API 密钥经 0600 临时配置文件传给 curl，避免出现在进程 argv
# （同机用户可经 ps / /proc/<pid>/cmdline 读取 argv）
curl_cfg=$(mktemp)
chmod 600 "$curl_cfg"
trap 'rm -f "$curl_cfg"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "$API_KEY" >"$curl_cfg"

log() {
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$WORKDIR/run.log"
}

list_sandbox_ids() {
  curl -fsS --max-time 15 -K "$curl_cfg" "$API_URL/sandboxes" 2>/dev/null |
    jq -r '.[]? | .sandboxID // .sandbox_id // .id' 2>/dev/null || true
}

delete_sandbox_ids() { # stdin: 每行一个沙箱 ID
  xargs -r -P 20 -I{} curl -fsS --max-time 60 -o /dev/null \
    -X DELETE -K "$curl_cfg" "$API_URL/sandboxes/{}" 2>/dev/null || true
}

snapshot_baseline_sandboxes() {
  local ids
  ids=$(list_sandbox_ids | sort -u | grep -v '^$' || true)
  printf '%s' "$ids"
}

cleanup_created_sandboxes() { # $1 = case 开始时的基线 ID 列表（可空）
  local baseline=$1 current created round
  for round in $(seq 1 10); do
    current=$(list_sandbox_ids | sort -u | grep -v '^$' || true)
    [[ -n "$current" ]] || return 0
    if [[ -n "$baseline" ]]; then
      created=$(comm -13 <(printf '%s\n' "$baseline" | sort -u) <(printf '%s\n' "$current"))
    else
      created=$current
    fi
    [[ -n "$created" ]] || return 0
    printf '%s\n' "$created" | delete_sandbox_ids
    sleep 2
  done
  current=$(list_sandbox_ids | sort -u | grep -v '^$' || true)
  if [[ -n "$current" ]]; then
    log "  WARNING: sandbox(es) created by this benchmark were not fully deleted:"
    printf '%s\n' "$current" | tee -a "$EVIDENCE_DIR/undeleted-sandboxes.txt" >&2
  fi
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
  # $1=concurrency $2=dirty_mb; 成功时输出 "wall_ms per_ms"，失败返回非 0
  local cc=$1 dirty=$2 out row wall per
  if ! out=$(CUBE_API_URL="$API_URL" CUBE_API_KEY="$API_KEY" CUBE_TEMPLATE_ID="$TEMPLATE_ID" \
    "${PYTHON_BIN}" "$BENCH_SCRIPT" -c "$cc" -n 1 -d "$dirty" 2>/dev/null); then
    return 1
  fi
  # 跳过表头/进度等非数据行，取最后一条 wall($3)/per($7) 均为纯数值的数据行
  row=$(printf '%s' "$out" | awk \
    'NF>=7 && $3 ~ /^[0-9]+([.][0-9]+)?$/ && $7 ~ /^[0-9]+([.][0-9]+)?$/ {line=$0} END {if (line != "") print line}')
  wall=$(printf '%s' "$row" | awk '{print $3}')
  per=$(printf '%s' "$row" | awk '{print $7}')
  [[ "$wall" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 1
  [[ "$per" =~ ^[0-9]+(\.[0-9]+)?$ ]] || per=$wall
  printf '%s %s\n' "$wall" "$per"
}

is_valid_case_name() {
  # 用例名参与构造输出路径，必须是单个文件名安全 token（不含 / 等目录分量）
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

FAILED=0
mapfile -t cases < <(printf '%s\n' "$CASE_MATRIX" | sed '/^[[:space:]]*$/d')
for case_line in "${cases[@]}"; do
  read -r name cc rounds dirty <<<"$case_line"
  cc=${cc:-1}; rounds=${rounds:-5}; dirty=${dirty:-0}
  if ! is_valid_case_name "$name"; then
    log "invalid case name '$name' (must be a single filename-safe token), skipping"
    FAILED=$((FAILED + 1))
    continue
  fi
  log "ROLLBACK_BEGIN name=${name} concurrency=${cc} rounds=${rounds} dirty_mb=${dirty}"

  baseline_sandboxes=$(snapshot_baseline_sandboxes)
  if [[ -n "$baseline_sandboxes" ]]; then
    leftover_count=$(printf '%s\n' "$baseline_sandboxes" | wc -l)
    printf '%s\n' "$baseline_sandboxes" >"$EVIDENCE_DIR/pre-existing-sandboxes-${name}.txt"
    if [[ "$ALLOW_GLOBAL_CLEANUP" == "1" ]]; then
      log "  ALLOW_GLOBAL_CLEANUP=1: deleting ${leftover_count} pre-existing sandbox(es) visible to this API key"
      printf '%s\n' "$baseline_sandboxes" | delete_sandbox_ids
    else
      log "  WARNING: ${leftover_count} pre-existing sandbox(es) visible to this API key are left untouched (set ALLOW_GLOBAL_CLEANUP=1 to remove them)"
    fi
  fi

  raw_file="$LATENCY_DIR/$name.json"
  summary_file="$LATENCY_DIR/$name.summary.json"
  rm -f "$raw_file" "$summary_file"
  raw_json='{"case":"'"$name"'","concurrency":'"$cc"',"rounds":'"$rounds"',"dirty_mb":'"$dirty"',"raw":[]}'

  wall_values=""; per_values=""; seq=0
  for i in $(seq 1 "$rounds"); do
    if ! out=$(run_one_round "$cc" "$dirty"); then
      log "  round $i failed for $name (bench exited non-zero or produced no parseable data row)"
      sleep 1
      continue
    fi
    read -r wall per <<<"$out"
    if ! [[ "$wall" =~ ^[0-9]+(\.[0-9]+)?$ && "$per" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      log "  round $i failed for $name (non-numeric wall/per values)"
      sleep 1
      continue
    fi
    seq=$((seq + 1))
    printf -v wall_values '%s\n%s' "$wall_values" "$wall"
    printf -v per_values '%s\n%s' "$per_values" "$per"
    new_raw=$(printf '%s' "$raw_json" | \
      jq --argjson seq "$seq" --argjson wall "$wall" --argjson per "$per" \
         '.raw += [{"seq":$seq,"wall_ms":$wall,"per_ms":$per}]' 2>/dev/null || true)
    if [[ -n "$new_raw" ]] && printf '%s' "$new_raw" | jq -e . >/dev/null 2>&1; then
      raw_json=$new_raw
    else
      log "  round $i: failed to append raw record for $name, keeping previous raw data"
    fi
    sleep 1
  done

  cleanup_created_sandboxes "$baseline_sandboxes"

  successful=$seq
  errors=$((rounds - successful))
  # 吞吐量 = 成功回滚请求数（每轮 concurrency 个并发回滚）/ 测量总耗时
  successful_rolls=$((successful * cc))
  total_time_s=$(printf '%s\n' "$wall_values" | grep -E '^[0-9]' | \
    awk '{s+=$1} END {if (NR==0) printf "0.000"; else printf "%.3f", s/1000}')
  throughput_rps=$(printf '%s\n' "$total_time_s" | \
    awk -v ok="$successful_rolls" '{if ($1>0) printf "%.3f", ok/$1; else printf "0.000"}')
  wall_stats=$(printf '%s\n' "$wall_values" | stats)
  per_stats=$(printf '%s\n' "$per_values" | stats)
  bench_exit=0
  if (( errors > 0 )); then bench_exit=1; FAILED=$((FAILED + 1)); fi

  success_rate=$(awk -v ok="$successful" -v total="$rounds" \
    'BEGIN { printf "%.3f", (total>0 ? ok/total : 0) }')

  printf '%s\n' "$raw_json" >"$raw_file"
  printf '{"case":"%s","concurrency":%s,"rounds":%s,"dirty_mb":%s,"bench_exit":%s,' \
    "$name" "$cc" "$rounds" "$dirty" "$bench_exit" \
    >"$summary_file"
  printf '"successful":%s,"errors":%s,"success_rate":%s,"total_time_s":%s,"throughput_rps":%s,' \
    "$successful" "$errors" "$success_rate" "$total_time_s" "$throughput_rps" \
    >>"$summary_file"
  printf '"wall_ms":%s,"per_rollback_ms":%s}\n' "$wall_stats" "$per_stats" \
    >>"$summary_file"

  log "ROLLBACK_END name=${name} successful=${successful} errors=${errors}"
done

exit "$FAILED"
