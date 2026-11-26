#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${1:-./output/cpu-affinity}"
BASE_DIR="${OUTPUT_DIR}"
DIAGNOSIS_DIR="${BASE_DIR}/diagnosis"
FINAL_DIR="${BASE_DIR}/final"
EVIDENCE_DIR="${BASE_DIR}/evidence"
ROLLBACK_DIR="${BASE_DIR}/rollback"

mkdir -p "${DIAGNOSIS_DIR}" "${FINAL_DIR}" "${EVIDENCE_DIR}" "${ROLLBACK_DIR}"

cat <<EOF
{
  "workspace_root": "${BASE_DIR}",
  "diagnosis_dir": "${DIAGNOSIS_DIR}",
  "final_dir": "${FINAL_DIR}",
  "evidence_dir": "${EVIDENCE_DIR}",
  "rollback_dir": "${ROLLBACK_DIR}"
}
EOF
