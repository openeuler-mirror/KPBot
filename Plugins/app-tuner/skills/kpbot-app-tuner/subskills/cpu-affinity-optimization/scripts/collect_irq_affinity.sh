#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common_functions.sh"

NIC_PATTERN="${1:-}"
OUTPUT_DIR="${2:-./output/cpu-affinity/diagnosis}"

mkdir -p "${OUTPUT_DIR}"
OUT_JSON="${OUTPUT_DIR}/irq_affinity.json"

if [[ ! -r /proc/interrupts ]]; then
  cat > "${OUT_JSON}" <<EOF
{"irq_cpu_conflict_notes":["interrupts_unavailable"],"fallback_notes":["proc_interrupts_unreadable"]}
EOF
  cat "${OUT_JSON}"
  exit 0
fi

if [[ -z "${NIC_PATTERN}" ]]; then
  MATCHED="$(grep -E 'eth|enp|ens|eno|bond' /proc/interrupts || true)"
else
  MATCHED="$(grep -E "${NIC_PATTERN}" /proc/interrupts || true)"
fi

if [[ -z "${MATCHED}" ]]; then
  cat > "${OUT_JSON}" <<EOF
{"irq_cpu_conflict_notes":["no_matching_irq_found"],"fallback_notes":[]}
EOF
  cat "${OUT_JSON}"
  exit 0
fi

NOTES="$(printf '%s\n' "${MATCHED}" | awk '{print $1" "$NF}' | json_array_from_lines)"

cat > "${OUT_JSON}" <<EOF
{
  "irq_cpu_conflict_notes": ${NOTES},
  "fallback_notes": []
}
EOF

cat "${OUT_JSON}"
