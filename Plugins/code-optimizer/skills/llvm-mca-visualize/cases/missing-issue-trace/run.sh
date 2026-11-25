#!/usr/bin/env bash
# Failure case: an unpatched llvm-mca (no -issue-trace) must be rejected with a
# non-zero exit, a success=false payload, and no HTML artifact.
set -uo pipefail

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
WORK="$CASE_DIR/results"
rm -rf "$WORK"
mkdir -p "$WORK"

MOCK="$WORK/mock-llvm-mca"
cat > "$MOCK" <<'SH'
#!/usr/bin/env bash
# Minimal stand-in for an llvm-mca build without the IssueTraceView patch.
case " $* " in
  *" --help "*) echo "USAGE: mock llvm-mca [options]"; exit 0 ;;
esac
echo "mock llvm-mca: unsupported invocation" >&2
exit 2
SH
chmod +x "$MOCK"

python3 "$SKILL_DIR/scripts/mca_visualize.py" \
    --asm "$SKILL_DIR/assets/examples/loop.s" \
    --uarch "${UARCH:-hip12}" \
    --mca-bin "$MOCK" \
    --out "$WORK/should_not_exist.html" > "$WORK/fail.log" 2>&1
rc=$?

python3 - "$WORK" "$rc" <<'PY'
import json
import os
import sys

work, rc = sys.argv[1], int(sys.argv[2])
log_path = os.path.join(work, "fail.log")
log = open(log_path, encoding="utf-8", errors="replace").read() if os.path.exists(log_path) else ""
html = os.path.join(work, "should_not_exist.html")

checks = [
    ("exit code is non-zero", rc != 0, rc),
    ("payload reports success=false", '"success": false' in log, ""),
    ("payload explains -issue-trace", "issue-trace" in log, ""),
    ("not an argparse error", "unrecognized arguments" not in log, ""),
    ("no HTML artifact", not os.path.exists(html), html),
]
failures = [{"name": n, "detail": d} for n, ok, d in checks if not ok]
result = {
    "success": not failures,
    "checks_ran": len(checks),
    "checks_failures": len(failures),
    "failures": failures,
    "observed": {"exit_code": rc, "html_created": os.path.exists(html)},
}
json.dump(result, open(os.path.join(work, "result.json"), "w"),
          ensure_ascii=False, indent=2)
for n, ok, _d in checks:
    print(("PASS " if ok else "FAIL ") + n)
sys.exit(0 if not failures else 1)
PY
