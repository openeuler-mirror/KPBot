#!/bin/bash
# Tier-A case runner for allocation-path-optimization.
#
# Drives the size-class audit and the allocation probe against synthetic
# fixtures, and checks the SKILL.md/output contract.  python3 is required; a C
# compiler is used when present for the probe numeric checks and skipped
# otherwise.
#
# Usage: bash cases/size-class-audit/run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$CASE_DIR/results"
mkdir -p "$RESULTS"

log() { echo "[case] $*"; }

OUT="$(python3 "$CASE_DIR/test_contract.py" 2>&1)"
RC=$?
echo "$OUT" | grep -v '^PYTEST_SUMMARY:'
SUMMARY="$(echo "$OUT" | grep '^PYTEST_SUMMARY:' | head -1 | cut -d: -f2-)"
if [ -z "$SUMMARY" ]; then
    log "FAIL: test harness produced no summary"
    SUMMARY='{"ran": 0, "failures": 1, "skipped": 0, "groups": {}, "failed_names": ["no summary"]}'
    RC=1
fi

python3 - "$RESULTS/result.json" "$SUMMARY" "$RC" <<'PY'
import json
import sys

out_path, summary_json, rc = sys.argv[1:4]
summary = json.loads(summary_json)
failures = int(summary.get("failures", 1))
ran = int(summary.get("ran", 0))
result = {
    "success": bool(failures == 0 and int(rc) == 0 and ran > 0),
    "checks_ran": ran,
    "checks_failures": failures,
    "checks_skipped": int(summary.get("skipped", 0)),
    "groups": summary.get("groups", {}),
    "failed_names": summary.get("failed_names", []),
}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
print(json.dumps(result, indent=2, ensure_ascii=False))
PY

log "result: $RESULTS/result.json"
python3 -c "import json,sys; d=json.load(open('$RESULTS/result.json')); sys.exit(0 if d['success'] else 1)"
