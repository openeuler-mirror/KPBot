#!/usr/bin/env bash
set -euo pipefail
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CASE_RESULTS="$CASE_DIR/results"
mkdir -p "$CASE_RESULTS"
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$CASE_DIR/test_contract.py"
