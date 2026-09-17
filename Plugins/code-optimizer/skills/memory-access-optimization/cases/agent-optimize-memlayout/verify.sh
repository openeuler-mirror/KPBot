#!/usr/bin/env bash
set -uo pipefail

cd /workspace
mkdir -p build results

SKILL_MODE="${SKILL_MODE:-with}"

gcc -O2 -std=c11 -Isrc bench/main.c src/kernel.c -lm -o build/baseline

if ! gcc -O2 -std=c11 -Isrc bench/main.c opt/kernel_opt.c -lm -o build/optimized 2>build/opt_compile.log; then
    SKILL_MODE="${SKILL_MODE}" python3 -c 'import json,os;json.dump({"success":False,"reason":"optimized compile failed","skill_mode":os.environ["SKILL_MODE"]},open("results/result.json","w"),indent=2)'
    exit 1
fi

N=${N:-256}
ITERS=${ITERS:-5}

./build/baseline "$N" "$ITERS" > results/baseline.out
base_rc=$?
./build/optimized "$N" "$ITERS" > results/optimized.out
opt_rc=$?

export SKILL_MODE="$SKILL_MODE"
export BASE_TIME="$(awk -F= '/^TIME_MS=/{print $2}' results/baseline.out)"
export OPT_TIME="$(awk -F= '/^TIME_MS=/{print $2}' results/optimized.out)"
export OPT_OK="$(awk -F= '/^CORRECT=/{print $2}' results/optimized.out)"
export MAX_REL_ERR="$(awk -F= '/^MAX_REL_ERR=/{print $2}' results/optimized.out)"
export OPT_RC="$opt_rc"

python3 - <<'PY'
import json, os
base_time = float(os.environ["BASE_TIME"] or 0)
opt_time = float(os.environ["OPT_TIME"] or 0)
speedup = round(base_time / opt_time, 4) if opt_time > 0 else 0.0
opt_ok = os.environ["OPT_OK"]
success = (os.environ["OPT_RC"] == "0" and opt_ok == "1")
json.dump({
    "success": success,
    "reason": "ok" if success else "correctness_or_run_failed",
    "skill_mode": os.environ["SKILL_MODE"],
    "optimized_correct": int(opt_ok),
    "max_rel_err": float(os.environ["MAX_REL_ERR"] or 0),
    "baseline_time_ms": base_time,
    "optimized_time_ms": opt_time,
    "speedup": speedup,
}, open("results/result.json", "w"), indent=2)
PY

if [ "${opt_rc}" -eq 0 ] && [ "${OPT_OK:-}" = "1" ]; then
    exit 0
else
    exit 1
fi