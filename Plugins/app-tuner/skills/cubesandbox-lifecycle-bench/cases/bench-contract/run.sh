#!/usr/bin/env bash
# Tier-A contract tests. No CubeSandbox deployment or root privileges required.
set -euo pipefail

CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${CASE_DIR}/../.." && pwd)"
RESULTS_DIR="${CASE_DIR}/results"
mkdir -p "${RESULTS_DIR}"

python3 -m py_compile \
  "${SKILL_DIR}/scripts/lib_cubesandbox.py" \
  "${SKILL_DIR}/scripts/cubesandbox_bench.py" \
  "${SKILL_DIR}/scripts/cubesandbox_skill.py"
python3 -m unittest discover -s "${CASE_DIR}" -p 'test_*.py' -v

python3 - "${RESULTS_DIR}/result.json" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump({
        "success": True,
        "scope": "plan/commands/approval/proxy/NUMA/TAP/dry-run",
        "requires_live_service": False,
    }, stream, indent=2)
    stream.write("\n")
PY
