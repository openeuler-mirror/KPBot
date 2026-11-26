#!/usr/bin/env bash
set -euo pipefail

REPORT_DIR="${1:-reports/current}"
SCENARIO_NAME="${2:-unnamed-scenario}"

mkdir -p "${REPORT_DIR}"

cat > "${REPORT_DIR}/metadata.txt" <<EOF
scenario_name=${SCENARIO_NAME}
status=initialized
EOF

cat > "${REPORT_DIR}/README.txt" <<'EOF'
This directory stores the report workspace for a server CPU optimization run.

Recommended contents:
- baseline/
- tuned/
- evidence/
- timing/
- final-report.md
EOF

mkdir -p "${REPORT_DIR}/baseline" "${REPORT_DIR}/tuned" "${REPORT_DIR}/evidence" "${REPORT_DIR}/timing"

cat > "${REPORT_DIR}/timing/README.txt" <<'EOF'
Store per-round optimization timing here.

Recommended fields for each round:
- analysis_duration
- implementation_duration
- validation_duration
- total_duration

Also store per-optimization timing details for the final report, for example:
- optimization_item
- analysis_duration
- implementation_duration
- validation_duration
- total_duration
EOF

echo "Initialized report workspace at ${REPORT_DIR}"
