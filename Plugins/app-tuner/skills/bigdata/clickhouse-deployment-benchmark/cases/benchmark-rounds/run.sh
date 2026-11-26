#!/usr/bin/env bash
set -eu
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$CASE_DIR/../.."
exec python3 "$CASE_DIR/test_benchmark_rounds.py"
