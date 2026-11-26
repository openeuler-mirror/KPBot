#!/usr/bin/env bash
set -euo pipefail

OUTPUT_ROOT="${1:-./output/cpu-affinity}"
DIAG_DIR="${OUTPUT_ROOT}/diagnosis"
OUT_FILE="${DIAG_DIR}/cpu_affinity_summary.json"

mkdir -p "${DIAG_DIR}"

read_json_or_default() {
  local path="$1"
  local default="$2"
  if [[ -f "${path}" ]]; then
    cat "${path}"
  else
    printf '%s' "${default}"
  fi
}

THREAD_AFFINITY="$(read_json_or_default "${DIAG_DIR}/thread_affinity.json" '{"thread_affinity":[],"fallback_notes":["thread_affinity_missing"]}')"
THREAD_DIST="$(read_json_or_default "${DIAG_DIR}/thread_distribution.json" '{"balance_status":"unknown","fallback_notes":["thread_distribution_missing"]}')"
THREAD_MIGRATION="$(read_json_or_default "${DIAG_DIR}/thread_migration.json" '{"migration_level":"unknown","fallback_notes":["thread_migration_missing"]}')"
IRQ_INFO="$(read_json_or_default "${DIAG_DIR}/irq_affinity.json" '{"irq_cpu_conflict_notes":["irq_affinity_missing"],"fallback_notes":["irq_affinity_missing"]}')"

cat > "${OUT_FILE}" <<EOF
{
  "generated_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')",
  "thread_affinity": ${THREAD_AFFINITY},
  "thread_distribution": ${THREAD_DIST},
  "thread_migration": ${THREAD_MIGRATION},
  "irq_affinity": ${IRQ_INFO}
}
EOF

cat "${OUT_FILE}"
