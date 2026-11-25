#!/usr/bin/env bash
# Guard checks: no patched llvm-mca needed (all failures happen before preflight).
set -uo pipefail

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
WORK="$CASE_DIR/results"
TOOL="$SKILL_DIR/scripts/mca_visualize.py"
rm -rf "$WORK"
mkdir -p "$WORK"

ran=0
fails=0

report() { # $1=ok(0/1) $2=name
    ran=$((ran + 1))
    if [ "$1" -eq 0 ]; then
        echo "PASS $2"
    else
        fails=$((fails + 1))
        echo "FAIL $2"
    fi
}

# 1) --out == input: refuse and keep the input bytes intact
cp "$SKILL_DIR/assets/examples/loop.s" "$WORK/in.s"
before="$(sha256sum "$WORK/in.s" | cut -d' ' -f1)"
python3 "$TOOL" --asm "$WORK/in.s" --out "$WORK/in.s" \
    --mca-bin "$WORK/no-mca" > "$WORK/out_same.log" 2>&1
rc=$?
after="$(sha256sum "$WORK/in.s" | cut -d' ' -f1)"
if [ "$rc" -ne 0 ] && grep -q '"success": false' "$WORK/out_same.log" \
   && [ "$before" = "$after" ]; then
    report 0 "out==input refused; input preserved"
else
    report 1 "out==input refused; input preserved"
fi

# 2) dangerous --cflags: rejected before clang is invoked
printf 'int f(int x){return x+1;}\n' > "$WORK/dummy.c"
python3 "$TOOL" --source "$WORK/dummy.c" --function f \
    --cflags -o "$WORK/redir.s" --mca-bin "$WORK/no-mca" \
    > "$WORK/cflags.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && grep -qi 'cflags' "$WORK/cflags.log" \
   && [ ! -e "$WORK/redir.s" ]; then
    report 0 "dangerous --cflags rejected"
else
    report 1 "dangerous --cflags rejected"
fi

# 3) CPU identity: implementer+part (Cortex-A53 must not map to hip10)
python3 "$SKILL_DIR/scripts/test/test_cpuinfo.py" > "$WORK/cpuinfo.log" 2>&1
report $? "cpuinfo implementer+part detection"

# 4) empty --parse: exit 1, no HTML
: > "$WORK/empty.txt"
python3 "$TOOL" --parse "$WORK/empty.txt" --out "$WORK/empty.html" \
    --mca-bin "$WORK/no-mca" > "$WORK/parse_empty.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && grep -q '"success": false' "$WORK/parse_empty.log" \
   && [ ! -e "$WORK/empty.html" ]; then
    report 0 "empty --parse fails without HTML"
else
    report 1 "empty --parse fails without HTML"
fi

# 5) malformed --parse (no "Timeline view:"): exit 1, no HTML
printf 'hello\n' > "$WORK/bad.txt"
python3 "$TOOL" --parse "$WORK/bad.txt" --out "$WORK/bad.html" \
    --mca-bin "$WORK/no-mca" > "$WORK/parse_bad.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && grep -q '"success": false' "$WORK/parse_bad.log" \
   && [ ! -e "$WORK/bad.html" ]; then
    report 0 "malformed --parse fails without HTML"
else
    report 1 "malformed --parse fails without HTML"
fi

# 6) JSON guard: invalid MCA --json output must die, not traceback
python3 "$SKILL_DIR/scripts/test/test_json_guard.py" > "$WORK/json_guard.log" 2>&1
report $? "invalid --json output fails cleanly"

# 7) cycle window boundary unit (records must be [0, C-1])
python3 "$SKILL_DIR/scripts/test/test_cycle_window.py" > "$WORK/cycle_window.log" 2>&1
report $? "cycle window boundary"

# 8) uarch tables must match llvm-mca-analysis (single source); SKIP is not allowed
python3 "$SKILL_DIR/scripts/test/test_uarch_table.py" > "$WORK/uarch_table.log" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && ! grep -q '^SKIP' "$WORK/uarch_table.log"; then
    report 0 "uarch table consistency with llvm-mca-analysis"
else
    report 1 "uarch table consistency with llvm-mca-analysis"
fi

# 9) --cflags allowlist (only confirmed optimization switches)
python3 "$SKILL_DIR/scripts/test/test_cflags_allowlist.py" > "$WORK/cflags_allow.log" 2>&1
report $? "cflags allowlist"

# 10) allowed cflags actually compile (skips when clang is absent)
python3 "$SKILL_DIR/scripts/test/test_cflags_compile.py" > "$WORK/cflags_compile.log" 2>&1
report $? "allowed cflags compile"

# 11) debug flag -g must be rejected end-to-end
python3 "$TOOL" --source "$WORK/dummy.c" --function f \
    --cflags -g --mca-bin "$WORK/no-mca" --out "$WORK/g.html" \
    > "$WORK/g.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ] && grep -q '"success": false' "$WORK/g.log" \
   && [ ! -e "$WORK/g.html" ]; then
    report 0 "-g rejected end-to-end"
else
    report 1 "-g rejected end-to-end"
fi

# 12) heatmap must not draw occupancy past the display window
python3 "$SKILL_DIR/scripts/test/test_heatmap_boundary.py" > "$WORK/heatmap_boundary.log" 2>&1
report $? "heatmap right-boundary clipping"

# 13) one-sided hip09 drift must be detected by the table guard
SIB="$SKILL_DIR/../llvm-mca-analysis/scripts/mca_analyze.py"
DRIFT="$WORK/mca_analyze_drift.py"
if [ -f "$SIB" ]; then
    python3 - "$SIB" "$DRIFT" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
text = text.replace('"hip09": "hip09"', '"hip09": "hip09-drift"')
open(dst, "w", encoding="utf-8").write(text)
PY
    python3 "$SKILL_DIR/scripts/test/test_uarch_table.py" --sibling "$DRIFT" \
        > "$WORK/uarch_drift.log" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ] && grep -q 'FAIL table UARCH_MAP' "$WORK/uarch_drift.log"; then
        report 0 "uarch table drift detected"
    else
        report 1 "uarch table drift detected"
    fi
else
    report 1 "uarch table drift detected (sibling missing)"
fi

python3 - "$WORK/result.json" "$ran" "$fails" <<'PY'
import json
import sys

path, ran, fails = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
json.dump({"success": fails == 0, "checks_ran": ran, "checks_failures": fails},
          open(path, "w"), ensure_ascii=False, indent=2)
PY

[ "$fails" -eq 0 ]
