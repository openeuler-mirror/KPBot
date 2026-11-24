#!/usr/bin/env bash
# Tier-A focused-skill contract: dry-run/mock only; never touches the host setup.
set -euo pipefail
CASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
COMMON_DIR="$SKILL_DIR/../cubesandbox-lifecycle-bench"
exec python3 "$COMMON_DIR/cases/focused-contract/test_contract.py" \
  --skill-dir "$SKILL_DIR" --results-dir "$CASE_DIR/results"
