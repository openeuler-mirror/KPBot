#!/usr/bin/env bash
# Backward-compatible CubeSandbox one-click benchmark collector.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
base_dir=${BASE_DIR:?set BASE_DIR to the CubeSandbox examples directory}
tools_dir=${TOOLS_DIR:-$script_dir/tools}
out_dir=${OUT_DIR:-$base_dir/profile-results}
runner=${RUNNER:-$tools_dir/run_cubesandbox_openeuler_template_perf.sh}
case_name=${CASE_NAME:-profile-c50-n500}
template_id=${TEMPLATE_ID:?set TEMPLATE_ID to the sandbox template ID}
: "${API_KEY:?set API_KEY to the CubeSandbox API key}"
export API_KEY
export BENCH=${BENCH:-$base_dir/cube-bench/bin/cube-bench}
log_dir=${LOG_DIR:-/data/log}
metrics_url=${METRICS_URL:-http://127.0.0.1:9998/v1/metrics}

if (($#)); then
  printf 'Usage: run_c50_profile.sh (configuration is supplied through environment variables)\n' >&2
  exit 2
fi

rm -f "$out_dir/run.log" 2>/dev/null || true
collect_args=(
  --out-dir "$out_dir"
  --log-dir "$log_dir"
  --metrics-url "$metrics_url"
  --tools-dir "$tools_dir"
  --host-duration "${HOST_DURATION:-12}"
  --host-interval "${HOST_INTERVAL:-0.1}"
  --process-samples "${PROCESS_SAMPLES:-9}"
  --process-interval "${PROCESS_INTERVAL:-1}"
  --trigger-file "$out_dir/run.log"
  --trigger-pattern "STARTUP_BEGIN name=$case_name "
  --trigger-timeout "${TRIGGER_TIMEOUT:-60}"
)
if [[ -n "${API_LOG:-}" ]]; then
  collect_args+=(--api-log "$API_LOG")
fi
case "${BPFTRACE_MODE:-auto}" in
  on) collect_args+=(--bpftrace) ;;
  off) collect_args+=(--no-bpftrace) ;;
  auto) collect_args+=(--bpftrace-auto) ;;
  *) printf 'ERROR: BPFTRACE_MODE must be on, off, or auto\n' >&2; exit 2 ;;
esac

WORKDIR="$out_dir" \
TEMPLATE_ID="$template_id" \
RUN_STARTUP=1 \
RUN_DENSITY=0 \
RETRY_UNTIL_SUCCESS=0 \
TAP_TARGET=1000 \
CASE_MATRIX="$case_name 50 500" \
  bash "$script_dir/collect_evidence.sh" "${collect_args[@]}" -- "$runner"
