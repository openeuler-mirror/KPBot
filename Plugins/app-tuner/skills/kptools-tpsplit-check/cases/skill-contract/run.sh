#!/bin/bash
# Tier-A case runner: skill-contract checks for kptools-tpsplit-check.
# Runs the bundled --selftest (report goes to a temp dir, never examples/),
# asserts workspace cleanliness, then writes results/result.json.
# Exit 0 only if the selftest is fully green and no tracked file changed.
#
# Usage: bash cases/skill-contract/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

log() { echo "[case] $*"; }

# ---------------- 0) python 解释器探测（python3 优先，回退 python） ----------------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[case] 错误：找不到 python3/python" >&2
  exit 1
fi

# ---------------- 0) snapshot workspace state (自测前) ----------------
# 只对比「自测新增的脏文件」：开发者未提交的既有改动不算自测弄脏工作区
snap_status() {
  if command -v git >/dev/null 2>&1 && git -C "$SKILL_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$SKILL_DIR" status --porcelain 2>/dev/null
  else
    echo ""
  fi
}
BEFORE="$(snap_status)"

# ---------------- 1) run --selftest and count checks ----------------
OUT="$("$PY" "$SKILL_DIR/scripts/analyze_feasibility.py" --selftest 2>&1)"
RC=$?
echo "$OUT"
RAN="$(echo "$OUT" | sed -n 's/^\([0-9][0-9]*\)\/[0-9][0-9]* passed$/\1/p' | tail -1)"
TOTAL="$(echo "$OUT" | sed -n 's/^[0-9][0-9]*\/\([0-9][0-9]*\) passed$/\1/p' | tail -1)"
[ -z "$RAN" ] && RAN=0
[ -z "$TOTAL" ] && TOTAL=0
FAILURES=$((TOTAL - RAN))
[ "$RC" -ne 0 ] && [ "$FAILURES" -eq 0 ] && FAILURES=1
log "selftest: ran=$RAN total=$TOTAL failures=$FAILURES rc=$RC"

# ---------------- 2) workspace cleanliness (selftest must not dirty tracked files) ----------------
# --selftest 把报告写进临时目录；跑完后的 git 状态应与跑之前一致（examples/ 不被改写）
AFTER="$(snap_status)"
EXAMPLES_CLEAN="True"
[ "$BEFORE" != "$AFTER" ] && EXAMPLES_CLEAN="False"
log "workspace: examples_clean=$EXAMPLES_CLEAN (before/after status一致)"
[ "$EXAMPLES_CLEAN" = "False" ] && FAILURES=$((FAILURES + 1))

# ---------------- 3) result.json ----------------
"$PY" - "$RESULTS_DIR/result.json" "$RAN" "$FAILURES" "$EXAMPLES_CLEAN" <<'PYEOF'
import json
import sys

out, ran, failures, examples_clean = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "True"
result = {
    "success": failures == 0 and ran > 0,
    "checks_ran": ran,
    "failures": failures,
    "examples_clean": examples_clean,
    "groups": {"selftest": {"ran": ran, "failures": failures}},
}
json.dump(result, open(out, "w"), ensure_ascii=False, indent=2)
print("[case] wrote %s: %s" % (out, result))
PYEOF

SUCCESS="$("$PY" -c "import json,sys;print(json.load(open('$RESULTS_DIR/result.json'))['success'])")"
[ "$SUCCESS" = "True" ] && exit 0 || exit 1
