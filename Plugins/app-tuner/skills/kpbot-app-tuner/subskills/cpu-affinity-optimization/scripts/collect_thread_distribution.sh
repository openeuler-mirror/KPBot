#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common_functions.sh"

PID="${1:-}"
OUTPUT_DIR="${2:-./output/cpu-affinity/diagnosis}"

mkdir -p "${OUTPUT_DIR}"

if [[ -z "${PID}" ]]; then
  write_json_error "target pid is required"
  exit 1
fi

RAW="$(ps -T -p "${PID}" -o psr= 2>/dev/null || true)"
OUT_JSON="${OUTPUT_DIR}/thread_distribution.json"

if [[ -z "${RAW}" ]]; then
  cat > "${OUT_JSON}" <<EOF
{"target_pid":"${PID}","balance_status":"unknown","hot_cpu_list":[],"thread_distribution":{},"fallback_notes":["ps_thread_view_empty"]}
EOF
  cat "${OUT_JSON}"
  exit 0
fi

COUNTS="$(printf '%s\n' "${RAW}" | awk 'NF{count[$1]++} END{for (cpu in count) printf "%s %d\n", cpu, count[cpu]}' | sort -n)"
MAX="$(printf '%s\n' "${COUNTS}" | awk 'BEGIN{max=0} {if ($2>max) max=$2} END{print max}')"
MIN="$(printf '%s\n' "${COUNTS}" | awk 'BEGIN{min=-1} {if (min==-1 || $2<min) min=$2} END{print (min==-1?0:min)}')"

if awk -v max="${MAX}" -v min="${MIN}" 'BEGIN { exit !((max-min) >= 2) }'; then
  STATUS="skewed"
  NOTES='["thread_distribution_is_skewed","hot_threads_concentrated_on_subset_of_cpus"]'
else
  STATUS="balanced"
  NOTES='["no_obvious_thread_cpu_skew_detected"]'
fi

DIST_JSON="$(printf '%s\n' "${COUNTS}" | awk 'BEGIN{printf "{"} {printf "%s\"%s\":%s", sep, $1, $2; sep=","} END{printf "}"}')"
HOT_JSON="$(printf '%s\n' "${COUNTS}" | awk -v max="${MAX}" 'BEGIN{printf "["} $2==max {printf "%s\"%s\"", sep, $1; sep=","} END{printf "]"}')"

cat > "${OUT_JSON}" <<EOF
{
  "target_pid": "${PID}",
  "balance_status": "${STATUS}",
  "hot_cpu_list": ${HOT_JSON},
  "thread_distribution": ${DIST_JSON},
  "thread_cpu_skew": {
    "max_threads_on_cpu": ${MAX},
    "min_threads_on_cpu": ${MIN}
  },
  "skew_notes": ${NOTES},
  "fallback_notes": []
}
EOF

cat "${OUT_JSON}"
