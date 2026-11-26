#!/bin/bash
# Tier-A case runner: skill-contract checks for kptools-tpsplit-optimization (L0 gate).
# Runs the bundled --selftest of references/l0_static_check.py (fixtures go to a
# temp dir, never the workspace), asserts workspace cleanliness, then writes
# results/result.json. Exit 0 only if the selftest is fully green and no
# tracked file changed.
#
# Usage: bash cases/skill-contract/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

log() { echo "[case] $*"; }

# ---------------- 0) python 解释器探测（python3 优先，回退 python；剔除 Store stub） ----------------
# Windows Git Bash 环境下 python3 可能命中 Microsoft Store 的 alias stub（打印
# "Python was not found" 并退出非零）——探测时先跑一句 -c 验证真正可用。
pick_py() {
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then
      echo "$cand"; return 0
    fi
  done
  return 1
}
if ! PY="$(pick_py)"; then
  echo "[case] 错误：找不到可用的 python3/python" >&2
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
OUT="$("$PY" "$SKILL_DIR/references/l0_static_check.py" --selftest 2>&1)"
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
# --selftest 把 fixture 写进临时目录；跑完后的 git 状态应与跑之前一致
AFTER="$(snap_status)"
WORKSPACE_CLEAN="True"
[ "$BEFORE" != "$AFTER" ] && WORKSPACE_CLEAN="False"
log "workspace: workspace_clean=$WORKSPACE_CLEAN (before/after status一致)"
[ "$WORKSPACE_CLEAN" = "False" ] && FAILURES=$((FAILURES + 1))

# ---------------- 3) result.json ----------------
"$PY" - "$RESULTS_DIR/result.json" "$RAN" "$FAILURES" "$WORKSPACE_CLEAN" <<'PYEOF'
import json
import sys

out, ran, failures, workspace_clean = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "True"
result = {
    "success": failures == 0 and ran > 0,
    "checks_ran": ran,
    "failures": failures,
    "workspace_clean": workspace_clean,
    "groups": {"selftest": {"ran": ran, "failures": failures}},
}
json.dump(result, open(out, "w"), ensure_ascii=False, indent=2)
print("[case] wrote %s: %s" % (out, result))
PYEOF

SUCCESS="$("$PY" - "$RESULTS_DIR/result.json" <<'PYEOF2'
import json, sys
print(json.load(open(sys.argv[1]))["success"])
PYEOF2
)"
[ "$SUCCESS" = "True" ] && exit 0 || exit 1
