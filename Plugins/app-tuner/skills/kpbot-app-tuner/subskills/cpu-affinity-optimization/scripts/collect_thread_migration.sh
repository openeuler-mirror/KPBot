#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common_functions.sh"

PID="${1:-}"
DURATION="${2:-5}"
OUTPUT_DIR="${3:-./output/cpu-affinity/diagnosis}"

mkdir -p "${OUTPUT_DIR}"
OUT_JSON="${OUTPUT_DIR}/thread_migration.json"

if [[ -z "${PID}" ]]; then
  write_json_error "target pid is required"
  exit 1
fi

if ! have_cmd perf; then
  cat > "${OUT_JSON}" <<EOF
{"target_pid":"${PID}","migration_level":"unknown","fallback_notes":["perf_missing"],"migration_count":0}
EOF
  cat "${OUT_JSON}"
  exit 0
fi

RAW_FILE="${OUTPUT_DIR}/thread_migration.txt"
timeout "$((DURATION + 2))" perf stat -e sched:sched_migrate_task -p "${PID}" sleep "${DURATION}" >"${RAW_FILE}" 2>&1 || true

MIGRATION_COUNT="$(grep -E 'sched(:|_)sched_migrate_task|sched_migrate_task' "${RAW_FILE}" | awk '{print $1}' | tr -d ',' | awk 'END{print ($1==""?0:$1)}')"
MIGRATION_COUNT="${MIGRATION_COUNT:-0}"

if [[ "${MIGRATION_COUNT}" -gt 100 ]]; then
  LEVEL="high"
elif [[ "${MIGRATION_COUNT}" -gt 10 ]]; then
  LEVEL="medium"
else
  LEVEL="low"
fi

cat > "${OUT_JSON}" <<EOF
{
  "target_pid": "${PID}",
  "duration_sec": ${DURATION},
  "migration_count": ${MIGRATION_COUNT},
  "migration_level": "${LEVEL}",
  "fallback_notes": []
}
EOF

cat "${OUT_JSON}"
