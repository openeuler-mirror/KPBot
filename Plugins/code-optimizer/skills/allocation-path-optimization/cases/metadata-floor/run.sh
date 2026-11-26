#!/bin/bash
# Tier-B case runner for allocation-path-optimization.
#
# Builds the naive fixture and the exact-size fixture from the same harness,
# then verifies three things:
#   1. correctness (reference/append model on both variants),
#   2. the allocator footprint drops: bytes requested per drain workload must
#      fall by at least 8x (measured with scripts/alloc_probe.c, deterministic),
#   3. the drain and addref interfaces get faster (median of N fresh-process
#      runs), and the data path does not regress beyond 10%.
#
# Usage: bash cases/metadata-floor/run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
BUILD="$CASE_DIR/build"
RESULTS="$CASE_DIR/results"
LOG="$BUILD/build.log"
REPS="${REPS:-15}"
ITERS="${ITERS:-200000}"
CC_BIN="${CC:-cc}"

mkdir -p "$BUILD" "$RESULTS"
: > "$LOG"
# A stale result from an earlier run must never be mistaken for this run's
# verdict: drop it up front and only write a new one at the end.
rm -f "$RESULTS/result.json"

FAILED=0
log() { echo "[case] $*"; }
fail() { log "FAIL: $*"; FAILED=1; }

median() { python3 -c '
import statistics, sys
print(statistics.median([float(x) for x in sys.argv[1:]]))
' "$@"; }

# ---------------------------------------------------------------- build
"$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$CASE_DIR/bench/main.c" -o "$BUILD/main.o" >>"$LOG" 2>&1 \
    || { fail "harness build"; exit 1; }
"$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$CASE_DIR/src/chain.c" -o "$BUILD/chain_base.o" >>"$LOG" 2>&1 \
    || { fail "baseline build"; exit 1; }
"$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$CASE_DIR/opt/chain_opt.c" -o "$BUILD/chain_opt.o" >>"$LOG" 2>&1 \
    || { fail "optimised build"; exit 1; }
# The probe lives in the skill; the container image also copies it next to the case.
PROBE_SRC="$SKILL_DIR/scripts/alloc_probe.c"
[ -f "$PROBE_SRC" ] || PROBE_SRC="$CASE_DIR/scripts/alloc_probe.c"
"$CC_BIN" -O2 -I "$CASE_DIR/src" -c "$PROBE_SRC" -o "$BUILD/alloc_probe.o" >>"$LOG" 2>&1 \
    || { fail "alloc probe build ($PROBE_SRC)"; exit 1; }

link() {
    local lib="$1" out="$2"
    "$CC_BIN" "$BUILD/main.o" "$BUILD/$lib" "$BUILD/alloc_probe.o" \
        -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free \
        -o "$BUILD/$out" >>"$LOG" 2>&1
}
link chain_base.o bench_base || { fail "baseline link"; exit 1; }
link chain_opt.o bench_opt || { fail "optimised link"; exit 1; }

# ---------------------------------------------------------- correctness
for variant in base opt; do
    out="$("$BUILD/bench_$variant" 1000 append 2>&1)"
    echo "$out" >> "$LOG"
    echo "$out" | grep -q '^FAILURES 0$' || fail "$variant correctness (see $LOG)"
    while read -r line; do [ -n "$line" ] && log "  $variant: $line"; done < <(echo "$out" | grep '^CHECK ')
done

# ------------------------------------------------------ allocator bytes
bytes_of() {
    "$BUILD/bench_$1" "$ITERS" drain 2>&1 >/dev/null | sed -n 's/.*alloc_report bytes=\([0-9]*\).*/\1/p' | tail -1
}
BASE_BYTES="$(bytes_of base)"
OPT_BYTES="$(bytes_of opt)"
if [ -z "$BASE_BYTES" ] || [ -z "$OPT_BYTES" ] || [ "$OPT_BYTES" -eq 0 ]; then
    fail "allocation accounting produced no data"
    BASE_RATIO=0
else
    BASE_RATIO="$(python3 -c "print($BASE_BYTES / $OPT_BYTES)")"
    log "requested bytes: baseline=$BASE_BYTES optimised=$OPT_BYTES ratio=$(printf '%.2f' "$BASE_RATIO")"
fi

# ------------------------------------------------------------- timing
# Pin to a CPU when possible; fall back to unpinned when taskset or the
# requested CPU is unavailable, so machines with few CPUs still produce samples.
PIN=()
if command -v taskset >/dev/null 2>&1; then
    pin_cpu="${CPU:-$(( $(nproc 2>/dev/null || echo 1) - 1 ))}"
    if taskset -c "$pin_cpu" true >/dev/null 2>&1; then
        PIN=("$pin_cpu")
        log "pinned to cpu $pin_cpu"
    else
        log "taskset -c $pin_cpu unavailable; running unpinned"
    fi
else
    log "taskset not found; running unpinned"
fi

run_bench() {  # <binary> <iters> <interface>
    if [ "${#PIN[@]}" -gt 0 ]; then
        taskset -c "${PIN[0]}" "$1" "$2" "$3"
    else
        "$1" "$2" "$3"
    fi
}

declare -A MED_BASE MED_OPT BASE_VALS OPT_VALS
for interface in drain addref append; do
    base_vals=(); opt_vals=()
    # The data path costs only a few ns/op, so give it a longer run to keep the
    # median meaningful; the allocation-heavy interfaces are already long enough.
    iters_for="$ITERS"
    [ "$interface" = "append" ] && iters_for=$((ITERS * 5))
    for _ in $(seq "$REPS"); do
        base_vals+=("$(run_bench "$BUILD/bench_base" "$iters_for" "$interface" \
            | awk -v i="$interface" '$2==i {sub("ns_per_op=","",$3); print $3}')")
        opt_vals+=("$(run_bench "$BUILD/bench_opt" "$iters_for" "$interface" \
            | awk -v i="$interface" '$2==i {sub("ns_per_op=","",$3); print $3}')")
    done
    MED_BASE[$interface]="$(median "${base_vals[@]}")"
    MED_OPT[$interface]="$(median "${opt_vals[@]}")"
    BASE_VALS[$interface]="${base_vals[*]}"
    OPT_VALS[$interface]="${opt_vals[*]}"
    if [ -z "${MED_BASE[$interface]:-}" ] || [ -z "${MED_OPT[$interface]:-}" ]; then
        fail "timing produced no samples for $interface"
    fi
done
if [ "$FAILED" -ne 0 ]; then
    python3 - "$RESULTS/result.json" <<'PY'
import json, sys
payload = {"success": False, "skipped": False,
           "error": "timing produced no samples; see build.log"}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, ensure_ascii=False)
print(json.dumps(payload, indent=2, ensure_ascii=False))
PY
    exit 1
fi

# ------------------------------------------------------------- verdict
python3 - "$RESULTS/result.json" "$FAILED" "$BASE_RATIO" "${BASE_BYTES:-0}" "${OPT_BYTES:-0}" \
    "${BASE_VALS[drain]}" "${OPT_VALS[drain]}" \
    "${BASE_VALS[addref]}" "${OPT_VALS[addref]}" \
    "${BASE_VALS[append]}" "${OPT_VALS[append]}" <<'PY'
import json, statistics, sys

(out_path, failed, ratio, base_bytes, opt_bytes,
 b_drain, o_drain, b_add, o_add, b_app, o_app) = sys.argv[1:]

ratio = float(ratio or 0)

def summarise(base_str, opt_str):
    """Speedup is the median of per-pair ratios, not a ratio of medians: a slow
    drift that hits both members of a pair is cancelled out."""
    base = [float(x) for x in base_str.split()]
    opt = [float(x) for x in opt_str.split()]
    if not base or len(base) != len(opt):
        return {"baseline_ns": 0.0, "optimized_ns": 0.0, "speedup": 0.0}
    ratios = [b / o for b, o in zip(base, opt) if o]
    return {
        "baseline_ns": statistics.median(base),
        "optimized_ns": statistics.median(opt),
        "speedup": statistics.median(ratios) if ratios else 0.0,
        "pairs": len(ratios),
    }

interfaces = {
    "drain": summarise(b_drain, o_drain),
    "addref": summarise(b_add, o_add),
    "append": summarise(b_app, o_app),
}
drain_speed = interfaces["drain"]["speedup"]
addref_speed = interfaces["addref"]["speedup"]
append_speed = interfaces["append"]["speedup"]

correct = int(failed) == 0
gates = {
    "correctness": correct,
    "bytes_reduction_gte_8x": ratio >= 8.0,
    "drain_speedup_gte_1_5x": drain_speed >= 1.5,
    "addref_speedup_gte_2x": addref_speed >= 2.0,
    "data_path_no_regression_gt_10pct": append_speed >= 0.9,
}
result = {
    "success": all(gates.values()),
    "all_gates_passed": all(gates.values()),
    "correctness": 1 if correct else 0,
    "bytes_reduction": ratio,
    "baseline_bytes": int(base_bytes),
    "optimized_bytes": int(opt_bytes),
    "interfaces": interfaces,
    "gates": gates,
}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
print(json.dumps(result, indent=2, ensure_ascii=False))
PY

for interface in drain addref append; do
    log "$interface: ${MED_BASE[$interface]} -> ${MED_OPT[$interface]} ns/op"
done

[ "$FAILED" -ne 0 ] && exit 1
python3 -c "import json,sys; d=json.load(open('$RESULTS/result.json')); sys.exit(0 if d['success'] else 1)"
