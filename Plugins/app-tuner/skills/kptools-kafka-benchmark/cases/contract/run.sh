#!/usr/bin/env bash
set -eu
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
cd "$SKILL_DIR"
exec python3 "$CASE_DIR/test_benchmark_contract.py"
