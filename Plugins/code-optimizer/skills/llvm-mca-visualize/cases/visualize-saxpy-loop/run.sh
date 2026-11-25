#!/usr/bin/env bash
# Success case: render assets/examples/loop.s with the default options (30 iterations,
# timeline 6) and verify the HTML + JSON contract.
set -uo pipefail

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
WORK="$CASE_DIR/results"
rm -rf "$WORK"
mkdir -p "$WORK"

fail_json() {
    python3 - "$WORK/result.json" "$1" <<'PY'
import json, sys
json.dump({"success": False, "checks_ran": 1, "checks_failures": 1,
           "failures": [sys.argv[2]]}, open(sys.argv[1], "w"), indent=2)
PY
}

MCA_BIN="${MCA_BIN:-}"
if [ -z "$MCA_BIN" ] || [ ! -x "$MCA_BIN" ]; then
    fail_json "MCA_BIN is not set or not executable (point it at patched llvm-mca)"
    exit 1
fi
if ! "$MCA_BIN" --help 2>&1 | grep -q -- 'issue-trace'; then
    fail_json "MCA_BIN does not support -issue-trace (unpatched llvm-mca)"
    exit 1
fi

if [ -n "${UARCH:-}" ]; then
    UARG=(--uarch "$UARCH")
else
    UARG=()
fi

python3 "$SKILL_DIR/scripts/mca_visualize.py" \
    --asm "$SKILL_DIR/assets/examples/loop.s" \
    --mca-bin "$MCA_BIN" \
    ${UARG[@]+"${UARG[@]}"} \
    --out "$WORK/out.html" > "$WORK/run.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ] || [ ! -s "$WORK/out.html" ]; then
    cat "$WORK/run.log" >&2
    fail_json "visualize run failed (rc=$rc); see results/run.log"
    exit 1
fi

python3 "$SKILL_DIR/scripts/mca_visualize.py" \
    --asm "$SKILL_DIR/assets/examples/loop.s" \
    --mca-bin "$MCA_BIN" \
    ${UARG[@]+"${UARG[@]}"} \
    --json > "$WORK/out.json" 2> "$WORK/run_json.err"
jrc=$?
if [ "$jrc" -ne 0 ] || [ ! -s "$WORK/out.json" ]; then
    fail_json "JSON run failed (rc=$jrc); see results/run_json.err"
    exit 1
fi

python3 "$CASE_DIR/verify.py" "$WORK/out.html" "$WORK/out.json" "$WORK/result.json"
exit $?
