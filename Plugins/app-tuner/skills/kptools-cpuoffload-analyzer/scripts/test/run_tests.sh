#!/usr/bin/env bash
# kptools-cpuoffload-analyzer 回归测试 — 自造 bundle fixture
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ANALYZE="$SCRIPT_DIR/scripts/analyze.py"

pick_py() {
  if [ -n "${AT_PY:-}" ] && "${AT_PY}" -c 'pass' >/dev/null 2>&1; then
    echo "${AT_PY}"; return
  fi
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'pass' >/dev/null 2>&1; then
      command -v "$c"; return
    fi
  done
  echo ""
}
PY="$(pick_py)"
[ -n "$PY" ] || { echo "FATAL: 需要可用的 python3"; exit 1; }
echo "python: $PY"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expect=[$2] got=[$3])"; fi; }
check_in() { case "$3" in *"$2"*) ok "$1";; *) bad "$1 (missing [$2])";; esac; }

# ---------------------------------------------------------------- fixture
mk_bundle() { # dir
  local d=$1
  mkdir -p "$d"
  # probe.after_init.json: rank0 2.0 GiB, rank1 1.5 GiB → 总 3.5
  "$PY" - "$d/probe.after_init.json" <<'PYEOF'
import json, sys
d = {
  "skill": "kptools-cpuoffload-collector", "tag": "after_init",
  "ts": 1795400000, "pfn_readable": True,
  "ranks": [
    {"pid": 1000, "rank": 0, "total_present_kb": 2*1048576,
     "total_va_kb": 4*1048576,
     "node_kb": {"0": 1.6*1048576, "1": 0.4*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*2*1048576, "present_kb": 2*1048576,
               "node_kb": {"0": 1.6*1048576, "1": 0.4*1048576}}]},
    {"pid": 1001, "rank": 1, "total_present_kb": 1.5*1048576,
     "total_va_kb": 2*1048576,
     "node_kb": {"1": 1.5*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*1048576, "present_kb": 1.5*1048576,
               "node_kb": {"1": 1.5*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
  # probe.step_1.json: +1.0 GiB（nova slab 首次）
  "$PY" - "$d/probe.step_1.json" <<'PYEOF'
import json, sys
d = {
  "skill": "kptools-cpuoffload-collector", "tag": "step_1",
  "ts": 1795400100, "pfn_readable": True,
  "ranks": [
    {"pid": 1000, "rank": 0, "total_present_kb": 3*1048576,
     "total_va_kb": 5*1048576,
     "node_kb": {"0": 2.4*1048576, "1": 0.6*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1400000, "pathname": "/dev/devmm_svm",
               "size_kb": 4*1048576, "present_kb": 3*1048576,
               "node_kb": {"0": 2.4*1048576, "1": 0.6*1048576}}]},
    {"pid": 1001, "rank": 1, "total_present_kb": 1.5*1048576,
     "total_va_kb": 2*1048576,
     "node_kb": {"1": 1.5*1048576},
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 3*1048576, "present_kb": 1.5*1048576,
               "node_kb": {"1": 1.5*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
}

# ============================================================ T1 基本分析
B1="$WORK/b1"; mk_bundle "$B1"
"$PY" "$ANALYZE" "$B1" >/dev/null 2>&1
v=$?
check_eq "T1a 基本分析 rc=0" "0" "$v"
[ -f "$B1/summary.json" ] && ok "T1b summary.json 生成" || bad "T1b summary.json 缺失"
v=$("$PY" - "$B1/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d["probes"], d["self_checks"]["passed"], len(d["findings"]),
       round(d["timeline"][-1]["total_gib"], 1))
PYEOF
)
check_eq "T1c 2 快照/自检通过/findings/总量" "['after_init', 'step_1'] True 1 4.5" "$v"

# ============================================================ T2 时间演化
v=$("$PY" - "$B1/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(round(d["timeline"][0]["total_gib"], 1), round(d["timeline"][1]["total_gib"], 1))
PYEOF
)
check_eq "T2 时间演化 after_init=3.5 step_1=4.5" "3.5 4.5" "$v"

# ============================================================ T3 nova 重建检测
v=$("$PY" - "$B1/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
nova = [f for f in d["findings"] if "nova" in f or "增量" in f]
print(len(nova))
PYEOF
)
check_eq "T3 nova 增量检测（+1 GiB）" "1" "$v"

# ============================================================ T4 自检失败场景
B4="$WORK/b4"; mkdir -p "$B4"
"$PY" - "$B4/probe.bad.json" <<'PYEOF'
import json, sys
d = {
  "tag": "bad", "ts": 1795400000, "pfn_readable": True,
  "ranks": [
    {"pid": 1000, "rank": 0, "total_present_kb": 2*1048576,
     "total_va_kb": 4*1048576,
     "node_kb": {"0": 1*1048576},   # 故意只给 1.0 GiB，与 total 2.0 不匹配
     "vmas": [{"start": 0x400000, "end": 0x1000000, "pathname": "/dev/devmm_svm",
               "size_kb": 6*1048576, "present_kb": 2*1048576,
               "node_kb": {"0": 1*1048576}}]}
  ]
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(d, fh, ensure_ascii=False, indent=2)
PYEOF
"$PY" "$ANALYZE" "$B4" >/dev/null 2>&1
v=$("$PY" - "$B4/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d["self_checks"]["passed"], len(d["self_checks"]["issues"]))
PYEOF
)
check_eq "T4 自检失败（node 不自洽，rank 级+VMA 级各触发）" "False 2" "$v"

# ============================================================ T5 退出码
"$PY" "$ANALYZE" "$WORK/empty" >/dev/null 2>&1
v=$?
check_eq "T5a 目录不存在 → 1" "1" "$v"
mkdir -p "$WORK/empty2"
"$PY" "$ANALYZE" "$WORK/empty2" >/dev/null 2>&1
v=$?
check_eq "T5b 空 bundle → 2" "2" "$v"

# ============================================================ T6 敏感自检
leak=$(grep -hoE '[a-f0-9]{32,}' "$SCRIPT_DIR/SKILL.md" 2>/dev/null | sort -u)
[ -z "$leak" ] && ok "T6 无 32+ hex 泄露" || bad "T6 泄露: $leak"

# ============================================================ 汇总
echo
echo "===== $PASS passed, $FAIL failed ====="
[ "$FAIL" -eq 0 ]
