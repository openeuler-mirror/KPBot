#!/usr/bin/env bash
set -eu
SKILL_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SKILL_DIR"
mkdir -p cases/analyze-records/results
exec python3 cases/analyze-records/test_tools.py --result cases/analyze-records/results/result.json
