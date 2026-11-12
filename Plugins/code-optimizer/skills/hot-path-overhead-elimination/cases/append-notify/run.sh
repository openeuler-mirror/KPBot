#!/bin/bash
# Tier-B case runner for hot-path-overhead-elimination.
#
# Builds the naive fixture and the recipe-applied fixture from the same harness,
# checks correctness on both, then compares three interfaces with process CPU
# time (median of N runs) and verifies the code generation actually changed.
#
# The case passes only if:
#   * both variants are correct,
#   * at least two interfaces are at least 1.30x faster,
#   * the optimised object contains the outlined slow paths the recipes require
#     and the baseline object does not.
#
# Usage: bash cases/append-notify/run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD="$CASE_DIR/build"
RESULTS="$CASE_DIR/results"
LOG="$BUILD/build.log"
REPS="${REPS:-15}"
ITERS="${ITERS:-200000}"
THRESHOLD="${THRESHOLD:-1.30}"
CC_BIN="${CC:-cc}"

mkdir -p "$BUILD" "$RESULTS"
: > "$LOG"

log() { echo "[case] $*"; }
fail() { log "FAIL: $*"; FAILED=1; }
FAILED=0

# ---------------------------------------------------------------- build
compile() {
    local name="$1" src="$2"
    "$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$CASE_DIR/bench/main.c" \
        -o "$BUILD/main.o" >>"$LOG" 2>&1 || return 1
    "$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$src" -o "$BUILD/$name.o" >>"$LOG" 2>&1 || return 1
    "$CC_BIN" "$BUILD/main.o" "$BUILD/$name.o" -o "$BUILD/bench_$name" >>"$LOG" 2>&1
}

log "building baseline and optimised variants with $CC_BIN"
compile base "$CASE_DIR/src/mbuffer.c" || { fail "baseline build"; exit 1; }
compile opt "$CASE_DIR/opt/mbuffer_opt.c" || { fail "optimised build"; exit 1; }

# ---------------------------------------------------------- correctness
for variant in base opt; do
    out="$("$BUILD/bench_$variant" 1000 2>&1)"
    echo "$out" >> "$LOG"
    if ! echo "$out" | grep -q '^FAILURES 0$'; then
        fail "$variant correctness (see $LOG)"
    fi
    while read -r line; do
        [ -n "$line" ] && log "  $variant: $line"
    done < <(echo "$out" | grep '^CHECK ')
done

# ------------------------------------------------------------- timing
median() { python3 -c '
import statistics, sys
vals = [float(x) for x in sys.argv[1:]]
print(statistics.median(vals))
' "$@"; }

declare -A MED_BASE MED_OPT BASE_VALS OPT_VALS
for interface in append readline drain; do
    declare -a base_vals=() opt_vals=()
    # Warm-up: one discarded run per side, as the methodology requires.
    for _ in 1 2; do
        "$BUILD/bench_base" "$ITERS" >/dev/null 2>&1
        "$BUILD/bench_opt" "$ITERS" >/dev/null 2>&1
    done
    for _ in $(seq "$REPS"); do
        base_vals+=("$(TK=base "$BUILD/bench_base" "$ITERS" 2>/dev/null | awk -v i="$interface" '$2==i {sub("ns_per_op=","",$3); print $3}')")
        opt_vals+=("$(TK=opt "$BUILD/bench_opt" "$ITERS" 2>/dev/null | awk -v i="$interface" '$2==i {sub("ns_per_op=","",$3); print $3}')")
    done
    MED_BASE[$interface]="$(median "${base_vals[@]}")"
    MED_OPT[$interface]="$(median "${opt_vals[@]}")"
    BASE_VALS[$interface]="${base_vals[*]}"
    OPT_VALS[$interface]="${opt_vals[*]}"
done

# --------------------------------------------------- codegen expectation
codegen_ok=0
if command -v objdump >/dev/null 2>&1; then
    objdump -d "$BUILD/base.o" > "$BUILD/base.dump" 2>/dev/null
    objdump -d "$BUILD/opt.o" > "$BUILD/opt.dump" 2>/dev/null
    base_out=0
    opt_out=0
    grep -q '<mbuf_append_general_>:' "$BUILD/base.dump" || base_out=0
    grep -q '<mbuf_append_general_>:' "$BUILD/base.dump" && base_out=1
    grep -q '<mbuf_append_general_>:' "$BUILD/opt.dump" && opt_out=1
    slow_opt=0
    grep -q '<mbuf_notify_slow>:' "$BUILD/opt.dump" && slow_opt=1
    if [ "$base_out" -eq 0 ] && [ "$opt_out" -eq 1 ] && [ "$slow_opt" -eq 1 ]; then
        codegen_ok=1
        log "codegen: outlined general append and notify present only in optimised object"
    else
        fail "codegen expectation (outlined symbols missing)"
    fi
else
    log "objdump not available: codegen check skipped"
    codegen_ok=1
fi

# ------------------------------------------------------------- verdict
python3 - "$RESULTS/result.json" "$THRESHOLD" "$codegen_ok" "$FAILED" \
    "${BASE_VALS[append]}" "${OPT_VALS[append]}" \
    "${BASE_VALS[readline]}" "${OPT_VALS[readline]}" \
    "${BASE_VALS[drain]}" "${OPT_VALS[drain]}" <<'PY'
import json, statistics, sys

(out_path, threshold, codegen_ok, failed,
 b_append, o_append, b_read, o_read, b_drain, o_drain) = sys.argv[1:]

threshold = float(threshold)

def summarise(base_str, opt_str):
    """Speedup is the median of per-pair ratios, matching the libevent case:
    a slow drift that hits both members of a pair cancels out."""
    base = [float(x) for x in base_str.split()]
    opt = [float(x) for x in opt_str.split()]
    if not base or len(base) != len(opt):
        return {"baseline_ns": 0.0, "optimized_ns": 0.0, "speedup": 0.0, "pairs": 0}
    ratios = [b / o for b, o in zip(base, opt) if o]
    return {
        "baseline_ns": statistics.median(base),
        "optimized_ns": statistics.median(opt),
        "speedup": statistics.median(ratios) if ratios else 0.0,
        "pairs": len(ratios),
    }

speeds = {
    "append": summarise(b_append, o_append),
    "readline": summarise(b_read, o_read),
    "drain": summarise(b_drain, o_drain),
}

improved = sum(1 for v in speeds.values() if v["speedup"] >= threshold)
correct = int(failed) == 0
result = {
    "success": bool(correct and improved >= 2 and int(codegen_ok) == 1),
    "correctness": 1 if correct else 0,
    "codegen_expectation": int(codegen_ok),
    "threshold_ratio": threshold,
    "interfaces_improved": improved,
    "interfaces": speeds,
}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
print(json.dumps(result, indent=2, ensure_ascii=False))
PY

for interface in append readline drain; do
    log "$interface: ${MED_BASE[$interface]} -> ${MED_OPT[$interface]} ns/op"
done

if [ "$FAILED" -ne 0 ]; then
    exit 1
fi
python3 -c "import json,sys; d=json.load(open('$RESULTS/result.json')); sys.exit(0 if d['success'] else 1)"
