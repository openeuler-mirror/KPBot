#!/bin/bash
# Tier-A case runner: skill-contract checks for vllm-eplb-deploy-validate.
# Runs the unittest contract suite plus the bundled script regression, then
# writes results/result.json. Exit 0 only if every group is failure-free.
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

# ---------------- script regression (counts its own checks) ----------------
CHK_OUT="$(cd "$SKILL_DIR" && python3 evals/run_checks.py 2>&1)"
CHK_RC=$?
echo "$CHK_OUT"
CHK_RAN="$(echo "$CHK_OUT" | sed -n 's#^\([0-9][0-9]*\)/[0-9][0-9]* checks passed#\1#p' | tail -1)"
CHK_TOTAL="$(echo "$CHK_OUT" | sed -n 's#^[0-9][0-9]*/\([0-9][0-9]*\) checks passed#\1#p' | tail -1)"
[ -z "$CHK_RAN" ] && CHK_RAN=0
[ -z "$CHK_TOTAL" ] && CHK_TOTAL=0
CHK_FAIL=$((CHK_TOTAL - CHK_RAN))
[ "$CHK_RC" -ne 0 ] && [ "$CHK_FAIL" -eq 0 ] && CHK_FAIL=1
log "scripts: ran=$CHK_RAN failed=$CHK_FAIL rc=$CHK_RC"

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

SUCCESS="$(python3 -c "import json,sys;print(json.load(open('$RESULTS_DIR/result.json'))['success'])")"
[ "$SUCCESS" = "True" ] && exit 0 || exit 1
