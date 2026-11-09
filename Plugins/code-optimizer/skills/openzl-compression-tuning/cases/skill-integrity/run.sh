#!/bin/bash
# Tier-A case runner: structural and content integrity checks for the
# openzl-compression-tuning knowledge skill (see checks.py for the list).
#
# Usage: bash cases/skill-integrity/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$CASE_DIR/results"

python3 "$CASE_DIR/checks.py"
RC=$?

echo "[case] result: $CASE_DIR/results/result.json"
exit $RC
