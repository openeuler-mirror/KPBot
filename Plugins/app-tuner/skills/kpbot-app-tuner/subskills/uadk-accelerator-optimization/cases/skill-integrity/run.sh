#!/bin/bash
# Tier-A case runner: skill-integrity checks for uadk-accelerator-optimization.
# Groups: static (bash: frontmatter/reference/JSON/行尾) + integrity (python unittest).
# Writes results/result.json. Exit 0 only if every group is failure-free.
#
# Usage: bash cases/skill-integrity/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

declare -i STATIC_RAN=0 STATIC_FAIL=0

log() { echo "[case] $*"; }
check() { STATIC_RAN+=1; if [ "$2" -ne 0 ]; then STATIC_FAIL+=1; log "FAIL: $1"; else log "ok: $1"; fi }

# ---------------- static checks (bash) ----------------
[ "$(head -1 "$SKILL_DIR/SKILL.md")" = "---" ]
check "frontmatter first line is ---" $?

for f in references/uadk-algorithm-map.json references/uadk-playbook.md; do
    test -f "$SKILL_DIR/$f"
    check "reference exists: $f" $?
done

python3 -m json.tool "$SKILL_DIR/references/uadk-algorithm-map.json" >/dev/null 2>&1
check "algorithm-map JSON parses" $?

for f in "$SKILL_DIR"/evals/*.json; do
    python3 - "$f" <<'PYEOF'
import json, re, sys
path = sys.argv[1]
raw = open(path, "rb").read().decode("utf-8")
json.loads(raw)
assert "\ufffd" not in raw, "U+FFFD found"
assert not re.search(r"\?{2,}", raw), "mojibake '?' runs found"
PYEOF
    check "evals utf8/mojibake scan: $(basename "$f")" $?
done

CR=0
for f in "$SKILL_DIR/SKILL.md" "$SKILL_DIR"/references/*.json "$SKILL_DIR"/references/*.md; do
    grep -q $'\r' "$f" && CR=1
done
check "skill files are LF-only" "$CR"

# ---------------- integrity (python unittest) ----------------
OUT="$(python3 "$CASE_DIR/test_skill_contract.py" 2>&1)"
RC=$?
echo "$OUT"
RAN="$(echo "$OUT" | sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' | tail -1)"
[ -z "$RAN" ] && RAN=0
INTEG_FAIL=0
[ "$RC" -ne 0 ] && INTEG_FAIL=1
log "integrity: ran=$RAN rc=$RC"

# ---------------- result.json ----------------
SUCCESS=false
if [ "$STATIC_FAIL" -eq 0 ] && [ "$INTEG_FAIL" -eq 0 ] && [ "$RAN" -gt 0 ]; then
    SUCCESS=true
fi

python3 - "$RESULTS_DIR/result.json" "$SUCCESS" "$STATIC_RAN" "$STATIC_FAIL" "$RAN" "$INTEG_FAIL" <<'EOF'
import json
import sys

(path, success, static_ran, static_fail, integ_ran, integ_fail) = sys.argv[1:7]
result = {
    "success": success == "true",
    "static_ran": int(static_ran),
    "static_failures": int(static_fail),
    "integrity_ran": int(integ_ran),
    "integrity_failures": int(integ_fail),
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
EOF

log "result: $RESULTS_DIR/result.json"
[ "$SUCCESS" = true ] && exit 0
exit 1
