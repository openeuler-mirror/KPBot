#!/bin/bash
# Tier-A case runner: skill-contract + script regressions for model-precision-analysis.
# Runs the unittest contract suite plus the extraction / pipeline / json-output
# regression scripts, then writes results/result.json. Exit 0 only if every group
# is failure-free.
#
# Usage: bash cases/skill-contract/run.sh   (from anywhere)
set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

log() { echo "[case] $*"; }

# ---------------- contract tests (python unittest) ----------------
OUT="$(python3 "$CASE_DIR/test_skill_contract.py" 2>&1)"
RC=$?
echo "$OUT"
UNIT_RAN="$(echo "$OUT" | sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' | tail -1)"
[ -z "$UNIT_RAN" ] && UNIT_RAN=0
UNIT_FAIL=0
[ "$RC" -ne 0 ] && UNIT_FAIL=1
log "contract: ran=$UNIT_RAN rc=$RC"

# ---------------- script regressions ----------------
run_group() {
  local script="$1" label="$2"
  local out rc ran total fail
  out="$(cd "$SKILL_DIR" && python3 "$script" 2>&1)"
  rc=$?
  echo "$out"
  ran="$(echo "$out" | sed -n "s#^\([0-9][0-9]*\)/[0-9][0-9]* $label.*#\1#p" | tail -1)"
  total="$(echo "$out" | sed -n "s#^[0-9][0-9]*/\([0-9][0-9]*\) $label.*#\1#p" | tail -1)"
  [ -z "$ran" ] && ran=0
  [ -z "$total" ] && total=0
  fail=$((total - ran))
  [ "$rc" -ne 0 ] && [ "$fail" -eq 0 ] && fail=1
  log "$script: ran=$ran failed=$fail rc=$rc"
  CHK_RAN=$((CHK_RAN + ran))
  CHK_TOTAL=$((CHK_TOTAL + total))
  CHK_FAIL=$((CHK_FAIL + fail))
}

CHK_RAN=0; CHK_TOTAL=0; CHK_FAIL=0
run_group tests/test_extraction.py "extraction cases passed"
run_group tests/test_pipeline.py "end-to-end cases passed"
run_group tests/test_json_output.py "json-output checks passed"

# ---------------- result.json ----------------
python3 - "$RESULTS_DIR/result.json" "$UNIT_RAN" "$UNIT_FAIL" "$CHK_RAN" "$CHK_FAIL" <<'PYEOF'
import json
import sys

out, unit_ran, unit_fail, chk_ran, chk_fail = sys.argv[1], *map(int, sys.argv[2:6])
checks_ran = unit_ran + chk_ran
failures = unit_fail + chk_fail
result = {
    "success": failures == 0 and checks_ran > 0,
    "checks_ran": checks_ran,
    "failures": failures,
    "groups": {
        "contract": {"ran": unit_ran, "failures": unit_fail},
        "scripts": {"ran": chk_ran, "failures": chk_fail},
    },
}
json.dump(result, open(out, "w"), ensure_ascii=False, indent=2)
print("[case] wrote %s: %s" % (out, result))
PYEOF

SUCCESS="$(python3 -c "import json;print(json.load(open('$RESULTS_DIR/result.json'))['success'])")"
[ "$SUCCESS" = "True" ] && exit 0 || exit 1
