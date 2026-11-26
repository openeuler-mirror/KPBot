#!/bin/bash
# Tier-A case runner: structural + de-privatization integrity checks for this skill.
# Usage: bash cases/skill-integrity/run.sh   (from anywhere)
set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$CASE_DIR/results"
python3 "$CASE_DIR/checks.py"
RC=$?
echo "[case] result: $CASE_DIR/results/result.json"
exit $RC
