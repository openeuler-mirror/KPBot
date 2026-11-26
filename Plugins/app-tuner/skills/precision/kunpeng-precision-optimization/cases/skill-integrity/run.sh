#!/usr/bin/env bash
# Tier-A runner: structural and content integrity checks for
# kunpeng-precision-optimization (see checks.py).
#
# Usage: bash cases/skill-integrity/run.sh   (from the skill root)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$CASE_DIR/results"

python3 "$CASE_DIR/checks.py"
RC=$?

echo "[case] result: $CASE_DIR/results/result.json"
exit $RC
