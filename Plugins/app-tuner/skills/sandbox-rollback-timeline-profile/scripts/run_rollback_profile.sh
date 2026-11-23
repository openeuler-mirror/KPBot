#!/usr/bin/env bash
# run_rollback_profile.sh - 回滚时序图采集：日志偏移切割 + host 采样 + bpftrace + 触发回滚基准
#
# 与 sandbox-timeline-profile 的 run_c50_profile.sh 机制一致，仅基准从 cube-bench
# 换成 bench_rollback_concurrency.py（每个沙箱自建快照后并发回滚，按轮重复）。
#
# 用法: bash run_rollback_profile.sh
# 必填环境变量: BASE_DIR TEMPLATE_ID API_KEY
# 可选: OUT_DIR CASE_NAME LOG_DIR METRICS_URL BENCH_SCRIPT RUNNER PYTHON_BIN
#       SHIM_PATTERN VMM_PATTERN CASE_MATRIX

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
base_dir=${BASE_DIR:?set BASE_DIR to the CubeSandbox examples directory}
tools_dir=${TOOLS_DIR:-$script_dir/tools}
out_dir=${OUT_DIR:-$base_dir/rollback-profile-results}
runner=${RUNNER:-$tools_dir/run_rollback_perf.sh}
case_name=${CASE_NAME:-profile-rb-c1-n5}
template_id=${TEMPLATE_ID:?set TEMPLATE_ID to the sandbox template ID}
: "${API_KEY:?set API_KEY to the CubeSandbox API key}"
export API_KEY
export BENCH_SCRIPT=${BENCH_SCRIPT:-$base_dir/snapshot-rollback-clone/bench_rollback_concurrency.py}
export PYTHON_BIN=${PYTHON_BIN:-python3}
log_dir=${LOG_DIR:-/data/log}
metrics_url=${METRICS_URL:-http://127.0.0.1:9998/v1/metrics}
api_log=${API_LOG:-$(find "$log_dir/CubeAPI" -maxdepth 1 -type f -name 'cube-api-*.log' -printf '%T@ %p
' | sort -nr | head -1 | cut -d' ' -f2-)}

if [[ -z "$api_log" ]]; then
  printf 'no CubeAPI log found under %s/CubeAPI\n' "$log_dir" >&2
  exit 1
fi

mkdir -p "$out_dir/evidence"
rm -f "$out_dir/run.log"

for entry in \
  cubelet_req:"$log_dir/Cubelet/Cubelet-req.log" \
  cubelet_stat:"$log_dir/Cubelet/Cubelet-stat.log" \
  shim_req:"$log_dir/CubeShim/cube-shim-req.log" \
  shim_stat:"$log_dir/CubeShim/cube-shim-stat.log" \
  vmm:"$log_dir/CubeVmm/vmm.log" \
  master:"$log_dir/CubeMaster/cubemaster-req.log" \
  api:"$api_log"; do
  name=${entry%%:*}
  path=${entry#*:}
  stat -c "$name %n %s %i" "$path"
done >"$out_dir/evidence/log-offsets-before.txt"

curl -fsS "$metrics_url" >"$out_dir/evidence/metrics-before.prom" || true

(
  deadline=$((SECONDS + 60))
  while ! grep -qF "ROLLBACK_BEGIN name=${case_name} " "$out_dir/run.log" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      printf 'profile trigger timed out for case %s\n' "$case_name" \
        >"$out_dir/evidence/profile-trigger-error.txt"
      exit 1
    fi
    sleep 0.05
  done

  date -Is >"$out_dir/evidence/profile-triggered-at.txt"
  "$tools_dir/sample_host.sh" \
    "$out_dir/evidence/host-samples.csv" 12 0.1 &
  sampler_pid=$!

  bpftrace -o "$out_dir/evidence/bpftrace-profile.txt" -e '
    profile:hz:19 /comm == "cubelet" || comm == "cubemaster" || comm == "network-agent"/ {
      @service_kernel[comm, kstack] = count();
      @service_user[comm, ustack] = count();
    }
    profile:hz:19 /comm == "vcpu0" || comm == "vcpu1" || comm == "cloud-hypervis" || comm == "cube-vmm"/ {
      @vm_kernel[comm, kstack] = count();
    }
    interval:s:8 { exit(); }
  ' >"$out_dir/evidence/bpftrace-launch.log" 2>&1 &
  tracer_pid=$!

  for second in $(seq 0 8); do
    {
      printf 'snapshot_at=%s second=%s\n' "$(date -Is)" "$second"
      ps -eLo pid,tid,ppid,psr,stat,pcpu,pmem,comm,wchan:32,args \
        --sort=-pcpu | head -400 \
        | sed -E 's/-api-key [^ ]+/-api-key <redacted>/g; s/Authorization: Bearer [^ ]+/Authorization: Bearer <redacted>/g'
    } >"$out_dir/evidence/process-snapshot-$second.txt"
    sleep 1
  done

  wait "$sampler_pid" || true
  wait "$tracer_pid" || true
) &
watcher_pid=$!

set +e
WORKDIR="$out_dir" \
TEMPLATE_ID="$template_id" \
CASE_MATRIX="${CASE_MATRIX:-$case_name 1 5 0}" \
  "$runner"
runner_rc=$?
set -e

watcher_rc=0
wait "$watcher_pid" || watcher_rc=$?

curl -fsS "$metrics_url" >"$out_dir/evidence/metrics-after.prom" || true

: >"$out_dir/evidence/log-offsets-after.txt"
while read -r name log_path before_size before_inode; do
  current_size=$(stat -c %s "$log_path")
  current_inode=$(stat -c %i "$log_path")
  printf '%s %s %s %s\n' "$name" "$log_path" "$current_size" "$current_inode" \
    >>"$out_dir/evidence/log-offsets-after.txt"

  delta_path="$out_dir/evidence/$name-delta.log"
  if [[ "$current_inode" == "$before_inode" && "$current_size" -ge "$before_size" ]]; then
    tail -c "+$((before_size + 1))" "$log_path" >"$delta_path"
  else
    cp "$log_path" "$delta_path"
    printf '%s inode_or_size_changed before_size=%s before_inode=%s after_size=%s after_inode=%s\n' \
      "$name" "$before_size" "$before_inode" "$current_size" "$current_inode" \
      >>"$out_dir/evidence/log-rotation-warnings.txt"
  fi
done <"$out_dir/evidence/log-offsets-before.txt"

printf '%s\n' "$runner_rc" >"$out_dir/evidence/runner-exit-code.txt"
printf '%s\n' "$watcher_rc" >"$out_dir/evidence/watcher-exit-code.txt"

if (( runner_rc != 0 )); then
  exit "$runner_rc"
fi
exit "$watcher_rc"
