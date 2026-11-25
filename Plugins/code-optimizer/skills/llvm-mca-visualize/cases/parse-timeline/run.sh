#!/usr/bin/env bash
# Parse regression: instruction text starting with a grid char ("eor") must be
# kept whole, and must not add an extra execute cycle.
set -uo pipefail

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
WORK="$CASE_DIR/results"
TOOL="$SKILL_DIR/scripts/mca_visualize.py"
FIX="$CASE_DIR/fixtures/eor.txt"
rm -rf "$WORK"
mkdir -p "$WORK"

python3 "$TOOL" --parse "$FIX" --json --mca-bin "$WORK/no-mca" \
    > "$WORK/tl.json" 2> "$WORK/tl.err"
if [ $? -ne 0 ]; then
    cat "$WORK/tl.err" >&2
    python3 -c "import json,sys;json.dump({'success':False,'checks_ran':1,'checks_failures':1,'failures':[{'name':'parse json','detail':sys.argv[1]}]},open(sys.argv[2],'w'),ensure_ascii=False,indent=2)" "$(cat "$WORK/tl.err")" "$WORK/result.json"
    exit 1
fi

python3 "$TOOL" --parse "$FIX" --out "$WORK/out.html" --mca-bin "$WORK/no-mca" \
    > "$WORK/html.log" 2>&1
html_rc=$?

UNK="$CASE_DIR/fixtures/unknown.txt"
python3 "$TOOL" --parse "$UNK" --out "$WORK/unknown.html" --mca-bin "$WORK/no-mca" \
    > "$WORK/unknown.log" 2>&1
unk_rc=$?

python3 - "$WORK" "$html_rc" "$unk_rc" <<'PY'
import json
import os
import sys

work, html_rc, unk_rc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
data = json.load(open(work + "/tl.json", encoding="utf-8"))
tl = data.get("timeline") or {}
inst = (tl.get("instructions") or [{}])[0]
stages = inst.get("stages") or {}
exec_cycles = [c for c, s in stages.items() if s in ("execute", "executed")]
html = open(work + "/out.html", encoding="utf-8", errors="replace").read()
unk_log = (open(work + "/unknown.log", encoding="utf-8", errors="replace").read()
           if os.path.exists(work + "/unknown.log") else "")

checks = [
    ("--parse ok", data.get("success") is True and tl.get("ok") is True, ""),
    ("instruction text intact", inst.get("text") == "eor x0, x1, x2", inst.get("text")),
    ("no extra execute cycle", len(exec_cycles) == 9, len(exec_cycles)),
    ("dispatch/retire unchanged", inst.get("dispatch") == 0 and inst.get("retire") == 10,
     (inst.get("dispatch"), inst.get("retire"))),
    ("cycles_total == header cells", tl.get("cycles_total") == 11, tl.get("cycles_total")),
    ("HTML keeps mnemonic", html_rc == 0 and "eor x0, x1, x2" in html, ""),
    ("unknown active char rejected", unk_rc != 0, unk_rc),
    ("unknown char error text", "未知阶段字符" in unk_log, unk_log[:120]),
    ("unknown char produces no HTML", not os.path.exists(work + "/unknown.html"), ""),
]
failures = [{"name": n, "detail": str(d)} for n, ok, d in checks if not ok]
json.dump({"success": not failures, "checks_ran": len(checks),
           "checks_failures": len(failures), "failures": failures},
          open(work + "/result.json", "w"), ensure_ascii=False, indent=2)
for n, ok, _d in checks:
    print(("PASS " if ok else "FAIL ") + n)
sys.exit(0 if not failures else 1)
PY
