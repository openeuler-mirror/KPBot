#!/bin/bash
# Tier-A case runner: skill-integrity checks for ceph-block-storage-deployment.
# Groups: static (bash: frontmatter/name/domain/category/reference/JSON/BOM/行尾) + integrity (python unittest).
# Writes results/result.json. Exit 0 only if every group is failure-free.
#
# Usage: bash cases/skill-integrity/run.sh   (from anywhere)
#
# 说明（继承自单体拆分前版本）：
#   1. python 解释器解析 python3 → python → py -3（Windows Git Bash 下 python3
#      可能命中 Microsoft Store 占位 stub，返回 49）；
#   2. evals 乱码扫描先取 rc 再传给 check，避免 $(basename) 命令替换覆盖 $?；
#   3. 含 UTF-8 BOM 检查与 frontmatter name/domain/category 检查。

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

declare -i STATIC_RAN=0 STATIC_FAIL=0

log() { echo "[case] $*"; }
check() { STATIC_RAN+=1; if [ "$2" -ne 0 ]; then STATIC_FAIL+=1; log "FAIL: $1"; else log "ok: $1"; fi }

# ---------------- resolve python interpreter ----------------
PY_BIN=""
for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1; then
        if [ "$cand" = "py" ]; then
            "$cand" -3 -c "" >/dev/null 2>&1 && PY_BIN="$cand -3"
        else
            "$cand" -c "" >/dev/null 2>&1 && PY_BIN="$cand"
        fi
        [ -n "$PY_BIN" ] && break
    fi
done
if [ -z "$PY_BIN" ]; then
    log "FAIL: no usable python interpreter (python3/python/py)"
    exit 1
fi
log "python interpreter: $PY_BIN"

# ---------------- static checks (bash) ----------------
[ "$(head -1 "$SKILL_DIR/SKILL.md")" = "---" ]
check "frontmatter first line is ---" $?

$PY_BIN - "$SKILL_DIR/SKILL.md" <<'PYEOF'
import sys
text = open(sys.argv[1], "rb").read().decode("utf-8")
fm = text.split("\n---\n", 1)[0]
assert "name: ceph-block-storage-deployment\n" in fm, "frontmatter name must match skill directory"
assert "metadata:" in fm and "domain: distributed_storage" in fm, "metadata.domain must be distributed_storage"
assert "category: deployment" in fm, "metadata.category must be deployment"
PYEOF
check "SKILL.md frontmatter name/domain/category" $?

for f in references/block-storage-deployment.md; do
    test -f "$SKILL_DIR/$f"
    check "reference exists: $f" $?
done

for f in "$SKILL_DIR"/evals/*.json; do
    $PY_BIN - "$f" <<'PYEOF'
import json, re, sys
path = sys.argv[1]
raw = open(path, "rb").read().decode("utf-8")
json.loads(raw)
assert "\ufffd" not in raw, "U+FFFD found"
assert not re.search(r"\?{2,}", raw), "mojibake '?' runs found"
PYEOF
    rc=$?
    check "evals utf8/mojibake scan: $(basename "$f")" "$rc"
done

CR=0
for f in "$SKILL_DIR/SKILL.md" "$SKILL_DIR"/references/*.md "$SKILL_DIR"/evals/*.json; do
    grep -q $'\r' "$f" && CR=1
done
check "skill files are LF-only" "$CR"

BOM=0
for f in "$SKILL_DIR/SKILL.md" "$SKILL_DIR"/references/*.md "$SKILL_DIR"/evals/*.json; do
    head -c 3 "$f" | od -An -tx1 | grep -q "ef bb bf" && BOM=1
done
check "skill files are UTF-8 without BOM" "$BOM"

# ---------------- integrity (python unittest) ----------------
OUT="$($PY_BIN "$CASE_DIR/test_skill_contract.py" 2>&1)"
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

$PY_BIN - "$RESULTS_DIR/result.json" "$SUCCESS" "$STATIC_RAN" "$STATIC_FAIL" "$RAN" "$INTEG_FAIL" <<'EOF'
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
