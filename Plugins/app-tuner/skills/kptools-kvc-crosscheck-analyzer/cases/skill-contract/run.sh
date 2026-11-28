#!/bin/bash
# Tier-A case runner: skill-contract checks for kptools-kvc-crosscheck-analyzer.
# Runs the unittest contract suite, then writes results/result.json.
# Exit 0 only if every check is failure-free.
#
# Usage: bash cases/skill-contract/run.sh   (from anywhere)
#
# 本技能为纯 LLM 分析技能（无脚本），因此没有脚本回归组；
# 安装/发现注册面由仓库级 tests/test_codex_install.py 的计数回归守护，
# 增删本技能时须同步更新 tests/fixtures/codex-skill-counts.json 与 docs/codex.md。

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"

log() { echo "[case] $*"; }

# 探测可用解释器：须能真正执行（Windows 应用商店的 python 别名占位会被拒绝）
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
[ -z "$PY" ] && { log "no usable python interpreter found"; exit 1; }

# ---------------- contract tests (python unittest) ----------------
OUT="$("$PY" "$CASE_DIR/test_skill_contract.py" 2>&1)"
RC=$?
echo "$OUT"
UNIT_RAN="$(echo "$OUT" | sed -n 's/^Ran \([0-9][0-9]*\) tests.*/\1/p' | tail -1)"
[ -z "$UNIT_RAN" ] && UNIT_RAN=0
UNIT_FAIL=0
[ "$RC" -ne 0 ] && UNIT_FAIL=1
log "contract: ran=$UNIT_RAN rc=$RC"

# ---------------- result.json ----------------
"$PY" - "$RESULTS_DIR/result.json" "$UNIT_RAN" "$UNIT_FAIL" <<'PYEOF'
import json
import sys

out, unit_ran, unit_fail = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
result = {
    "success": unit_fail == 0 and unit_ran > 0,
    "checks_ran": unit_ran,
    "failures": unit_fail,
    "groups": {
        "contract": {"ran": unit_ran, "failures": unit_fail},
    },
}
json.dump(result, open(out, "w"), ensure_ascii=False, indent=2)
print("[case] wrote %s: %s" % (out, result))
PYEOF

# 路径作为参数传入而非内嵌进 -c 代码：Windows 下 Git Bash 会把 MSYS 路径参数
# 自动转换为本机路径，原生 python.exe 才能打开；Linux 下行为不变。
SUCCESS="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["success"])' "$RESULTS_DIR/result.json")"
[ "$SUCCESS" = "True" ] && exit 0 || exit 1
