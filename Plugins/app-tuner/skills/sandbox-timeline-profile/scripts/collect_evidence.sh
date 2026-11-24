#!/usr/bin/env bash
# Collect CubeSandbox evidence around an argv-preserving external workload.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out_dir=
log_dir=${LOG_DIR:-/data/log}
api_log=${API_LOG:-}
metrics_url=${METRICS_URL:-http://127.0.0.1:9998/v1/metrics}
metrics_enabled=1
tools_dir=${TOOLS_DIR:-$script_dir/tools}
host_duration=${HOST_DURATION:-12}
host_interval=${HOST_INTERVAL:-0.1}
process_samples=${PROCESS_SAMPLES:-9}
process_interval=${PROCESS_INTERVAL:-1}
bpftrace_mode=${BPFTRACE_MODE:-off}
trigger_file=
trigger_pattern=
trigger_timeout=${TRIGGER_TIMEOUT:-60}

usage() {
  cat <<'EOF'
Usage:
  collect_evidence.sh --out-dir DIR [options] -- COMMAND [ARG ...]

Options:
  --log-dir DIR             Component log root (default: /data/log)
  --api-log FILE            Explicit CubeAPI log
  --metrics-url URL         Metrics endpoint
  --no-metrics              Skip metrics snapshots explicitly
  --tools-dir DIR           Directory containing sample_c50_host.sh
  --host-duration SECONDS   Host sampler duration (default: 12)
  --host-interval SECONDS   Host sampler interval (default: 0.1)
  --process-samples N       Process snapshot count (default: 9)
  --process-interval SEC    Delay between process snapshots (default: 1)
  --bpftrace                Require and run bpftrace
  --bpftrace-auto           Run bpftrace only when available
  --no-bpftrace             Disable bpftrace (default for external workloads)
  --trigger-file FILE       Delay observers until PATTERN appears in FILE
  --trigger-pattern TEXT    Fixed-string trigger used with --trigger-file
  --trigger-timeout SEC     Trigger wait timeout (default: 60)
  -h, --help                Show this help

The command after -- is executed directly as argv; no shell eval is used.
EOF
}

while (($#)); do
  case "$1" in
    --out-dir) out_dir=${2:?"--out-dir requires a value"}; shift 2 ;;
    --log-dir) log_dir=${2:?"--log-dir requires a value"}; shift 2 ;;
    --api-log) api_log=${2:?"--api-log requires a value"}; shift 2 ;;
    --metrics-url) metrics_url=${2:?"--metrics-url requires a value"}; shift 2 ;;
    --no-metrics) metrics_enabled=0; shift ;;
    --tools-dir) tools_dir=${2:?"--tools-dir requires a value"}; shift 2 ;;
    --host-duration) host_duration=${2:?"--host-duration requires a value"}; shift 2 ;;
    --host-interval) host_interval=${2:?"--host-interval requires a value"}; shift 2 ;;
    --process-samples) process_samples=${2:?"--process-samples requires a value"}; shift 2 ;;
    --process-interval) process_interval=${2:?"--process-interval requires a value"}; shift 2 ;;
    --bpftrace) bpftrace_mode=on; shift ;;
    --bpftrace-auto) bpftrace_mode=auto; shift ;;
    --no-bpftrace) bpftrace_mode=off; shift ;;
    --trigger-file) trigger_file=${2:?"--trigger-file requires a value"}; shift 2 ;;
    --trigger-pattern) trigger_pattern=${2:?"--trigger-pattern requires a value"}; shift 2 ;;
    --trigger-timeout) trigger_timeout=${2:?"--trigger-timeout requires a value"}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$out_dir" || $# -eq 0 ]]; then
  printf 'ERROR: --out-dir and a command after -- are required\n' >&2
  usage >&2
  exit 2
fi
if [[ -n "$trigger_file" && -z "$trigger_pattern" ]]; then
  printf 'ERROR: --trigger-file requires --trigger-pattern\n' >&2
  exit 2
fi
if [[ "$bpftrace_mode" != on && "$bpftrace_mode" != auto && "$bpftrace_mode" != off ]]; then
  printf 'ERROR: invalid BPFTRACE_MODE: %s\n' "$bpftrace_mode" >&2
  exit 2
fi
if [[ ! -f "$tools_dir/sample_c50_host.sh" ]]; then
  printf 'BLOCKED: host sampler not found: %s/sample_c50_host.sh\n' "$tools_dir" >&2
  exit 2
fi
if [[ "$bpftrace_mode" == on ]] && ! command -v bpftrace >/dev/null 2>&1; then
  printf 'BLOCKED: --bpftrace requested but bpftrace is unavailable\n' >&2
  exit 2
fi
if (( metrics_enabled )) && ! command -v curl >/dev/null 2>&1; then
  printf 'BLOCKED: curl is required for metrics; use --no-metrics to opt out\n' >&2
  exit 2
fi

if [[ -z "$api_log" ]]; then
  api_log=$(find "$log_dir/CubeAPI" -maxdepth 1 -type f -name 'cube-api-*.log' \
    -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)
fi
if [[ -z "$api_log" ]]; then
  printf 'BLOCKED: no CubeAPI log found under %s/CubeAPI\n' "$log_dir" >&2
  exit 2
fi

mkdir -p "$out_dir/evidence"
evidence_dir=$out_dir/evidence
rm -f \
  "$evidence_dir"/log-rotation-warnings.txt \
  "$evidence_dir"/profile-trigger-error.txt \
  "$evidence_dir"/metrics-before-error.txt \
  "$evidence_dir"/metrics-after-error.txt \
  "$evidence_dir"/bpftrace-profile.txt \
  "$evidence_dir"/bpftrace-launch.log \
  "$evidence_dir"/bpftrace-exit-code.txt

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
  if [[ ! -f "$path" ]]; then
    printf 'BLOCKED: required log is missing: %s\n' "$path" >&2
    exit 2
  fi
  before_size=$(stat -c %s "$path")
  before_inode=$(stat -c %i "$path")
  printf '%s\t%s\t%s\t%s\n' "$name" "$path" "$before_size" "$before_inode"
done >"$evidence_dir/log-offsets-before.txt"

metrics_before_rc=0
if (( metrics_enabled )); then
  set +e
  curl -fsS "$metrics_url" >"$evidence_dir/metrics-before.prom"
  metrics_before_rc=$?
  set -e
  if (( metrics_before_rc != 0 )); then
    printf 'metrics-before collection failed (exit code %s)\n' "$metrics_before_rc" \
      >"$evidence_dir/metrics-before-error.txt"
  fi
else
  printf 'metrics collection disabled by --no-metrics\n' >"$evidence_dir/metrics-skipped.txt"
fi

use_bpftrace=0
if [[ "$bpftrace_mode" == on ]] || { [[ "$bpftrace_mode" == auto ]] && command -v bpftrace >/dev/null 2>&1; }; then
  use_bpftrace=1
else
  printf 'bpftrace mode=%s; no trace loaded\n' "$bpftrace_mode" >"$evidence_dir/bpftrace-skipped.txt"
fi

(
  set +e
  if [[ -n "$trigger_file" ]]; then
    deadline=$((SECONDS + trigger_timeout))
    while ! grep -qF "$trigger_pattern" "$trigger_file" 2>/dev/null; do
      if (( SECONDS >= deadline )); then
        printf 'profile trigger timed out waiting for %s in %s\n' "$trigger_pattern" "$trigger_file" \
          >"$evidence_dir/profile-trigger-error.txt"
        exit 1
      fi
      sleep 0.05
    done
  fi

  date -Is >"$evidence_dir/profile-triggered-at.txt"
  bash "$tools_dir/sample_c50_host.sh" \
    "$evidence_dir/host-samples.csv" "$host_duration" "$host_interval" &
  sampler_pid=$!

  tracer_pid=
  if (( use_bpftrace )); then
    bpftrace -o "$evidence_dir/bpftrace-profile.txt" -e '
      profile:hz:19 /comm == "cubelet" || comm == "cubemaster" || comm == "network-agent"/ {
        @service_kernel[comm, kstack] = count();
        @service_user[comm, ustack] = count();
      }
      profile:hz:19 /comm == "vcpu0" || comm == "vcpu1" || comm == "cloud-hypervis" || comm == "cube-vmm"/ {
        @vm_kernel[comm, kstack] = count();
      }
      interval:s:8 { exit(); }
    ' >"$evidence_dir/bpftrace-launch.log" 2>&1 &
    tracer_pid=$!
  fi

  for ((index = 0; index < process_samples; index += 1)); do
    {
      printf 'snapshot_at=%s second=%s\n' "$(date -Is)" "$index"
      ps -eLo pid,tid,ppid,psr,stat,pcpu,pmem,comm,wchan:32 \
        --sort=-pcpu 2>/dev/null | head -400
    } >"$evidence_dir/process-snapshot-$index.txt"
    if (( index + 1 < process_samples )); then
      sleep "$process_interval"
    fi
  done

  observer_rc=0
  wait "$sampler_pid"
  sampler_rc=$?
  printf '%s\n' "$sampler_rc" >"$evidence_dir/sampler-exit-code.txt"
  (( sampler_rc == 0 )) || observer_rc=1
  if [[ -n "$tracer_pid" ]]; then
    wait "$tracer_pid"
    tracer_rc=$?
    printf '%s\n' "$tracer_rc" >"$evidence_dir/bpftrace-exit-code.txt"
    if [[ "$bpftrace_mode" == on ]] && (( tracer_rc != 0 )); then
      observer_rc=1
    fi
  fi
  exit "$observer_rc"
) &
observer_pid=$!

set +e
"$@"
workload_rc=$?
set -e

observer_rc=0
wait "$observer_pid" || observer_rc=$?
metrics_after_rc=0
if (( metrics_enabled )); then
  set +e
  curl -fsS "$metrics_url" >"$evidence_dir/metrics-after.prom"
  metrics_after_rc=$?
  set -e
  if (( metrics_after_rc != 0 )); then
    printf 'metrics-after collection failed\n' >"$evidence_dir/metrics-after-error.txt"
  fi
fi

finalization_rc=0
: >"$evidence_dir/log-offsets-after.txt"
: >"$evidence_dir/log-rotation-warnings.txt"
while IFS=$'\t' read -r name log_path before_size before_inode; do
  if ! current_size=$(stat -c %s "$log_path") || ! current_inode=$(stat -c %i "$log_path"); then
    printf '%s log_missing_after_workload path=%s\n' "$name" "$log_path" \
      >>"$evidence_dir/log-rotation-warnings.txt"
    finalization_rc=1
    continue
  fi
  printf '%s\t%s\t%s\t%s\n' "$name" "$log_path" "$current_size" "$current_inode" \
    >>"$evidence_dir/log-offsets-after.txt"

  delta_path="$evidence_dir/$name-delta.log"
  if [[ "$current_inode" == "$before_inode" && "$current_size" -ge "$before_size" ]]; then
    tail -c "+$((before_size + 1))" "$log_path" >"$delta_path"
  else
    cp "$log_path" "$delta_path"
    printf '%s inode_or_size_changed before_size=%s before_inode=%s after_size=%s after_inode=%s\n' \
      "$name" "$before_size" "$before_inode" "$current_size" "$current_inode" \
      >>"$evidence_dir/log-rotation-warnings.txt"
  fi
done <"$evidence_dir/log-offsets-before.txt"

printf '%s\n' "$workload_rc" >"$evidence_dir/workload-exit-code.txt"
printf '%s\n' "$workload_rc" >"$evidence_dir/runner-exit-code.txt"
printf '%s\n' "$observer_rc" >"$evidence_dir/watcher-exit-code.txt"

python3 - "$evidence_dir" "$workload_rc" "$observer_rc" "$metrics_before_rc" "$metrics_after_rc" \
  "$finalization_rc" "$bpftrace_mode" "$@" <<'PY'
import datetime
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
workload_rc, observer_rc, metrics_before_rc, metrics_after_rc, finalization_rc = map(int, sys.argv[2:7])
bpftrace_mode = sys.argv[7]
argv = sys.argv[8:]

SENSITIVE_FLAGS = {"--api-key", "-api-key", "--token", "--password", "--authorization"}
def redact_argv(items):
    result = []
    redact_next = False
    for item in items:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
        elif item in SENSITIVE_FLAGS:
            result.append(item)
            redact_next = True
        elif any(item.startswith(flag + "=") for flag in SENSITIVE_FLAGS):
            result.append(item.split("=", 1)[0] + "=<redacted>")
        elif item.lower().startswith("authorization: bearer "):
            result.append("Authorization: Bearer <redacted>")
        else:
            result.append(item)
    return result

def read_offsets(name):
    path = root / name
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 4:
                rows[fields[0]] = {"path": fields[1], "size": int(fields[2]), "inode": int(fields[3])}
    return rows

before = read_offsets("log-offsets-before.txt")
after = read_offsets("log-offsets-after.txt")
logs = []
for name, old in before.items():
    new = after.get(name)
    logs.append({
        "name": name,
        "path": old["path"],
        "delta": f"{name}-delta.log",
        "before": old,
        "after": new,
        "rotation_fallback": new is None or new["inode"] != old["inode"] or new["size"] < old["size"],
    })

failed = any((workload_rc, observer_rc, metrics_before_rc, metrics_after_rc, finalization_rc))
manifest = {
    "schema_version": 1,
    "status": "failed" if failed else "success",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "workload": {"argv": redact_argv(argv), "exit_code": workload_rc, "exec_mode": "direct_argv_no_eval"},
    "observer_exit_code": observer_rc,
    "metrics_before_exit_code": metrics_before_rc,
    "metrics_after_exit_code": metrics_after_rc,
    "finalization_exit_code": finalization_rc,
    "bpftrace_mode": bpftrace_mode,
    "logs": logs,
    "artifacts": sorted(path.name for path in root.iterdir() if path.is_file()),
}
(root / "evidence-manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

if (( workload_rc != 0 )); then
  printf 'Workload failed with exit code %s; evidence retained in %s\n' "$workload_rc" "$evidence_dir" >&2
  exit "$workload_rc"
fi
if (( observer_rc != 0 || metrics_before_rc != 0 || metrics_after_rc != 0 || finalization_rc != 0 )); then
  printf 'Evidence collection incomplete; see %s/evidence-manifest.json\n' "$evidence_dir" >&2
  exit 1
fi
printf 'Evidence collected: %s\n' "$evidence_dir/evidence-manifest.json"
