#!/bin/bash
# Tier-C case runner for utf8-validation-neon.
#
# Drives test_contract.py: SKILL.md invariants + real-machine (aarch64)
# transform semantics against self-contained fixtures in src/, then writes
# results/result.json.  Non-aarch64 hosts or missing toolchains SKIP the
# transform group (recorded in result.json) instead of failing.
#
# Usage: bash cases/router-contract/run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$CASE_DIR/results"

python3 "$CASE_DIR/test_contract.py"
exit $?
