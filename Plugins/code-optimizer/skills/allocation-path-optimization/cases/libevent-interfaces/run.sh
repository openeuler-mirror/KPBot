#!/bin/bash
# Tier-B case runner: apply this skill's recipe to a real libevent checkout and
# verify the outcome on the allocator-bound interfaces.
#
# Requires a pristine libevent 2.1.12 source tree in LIBEVENT_SRC (this case
# cannot ship the library itself).  When the variable is unset or the tree does
# not look pristine, the case records skipped=true and exits 0 — it is a guard,
# not a silent pass: the log says exactly why nothing was verified.
#
# What it does:
#   1. copies the source twice: baseline and candidate;
#   2. applies recipes/0001-allocation-path-optimization.patch to the candidate;
#   3. builds both and runs the evbuffer regression group on the candidate;
#   4. compares drain / readline / append with alternating pairs and medians;
#   5. measures requested allocation bytes for the drain workload — the primary,
#      deterministic signal for this skill.
#
# Usage: LIBEVENT_SRC=/path/to/libevent-2.1.12 bash run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
WORK="$CASE_DIR/work"
RESULTS="$CASE_DIR/results"
LOG="$WORK/run.log"
REPS="${REPS:-7}"
# 15 pairs: the no-regression guard on the unchanged data path needs a stable
# median of per-pair ratios, and 7 pairs is too few on a shared host.
PAIRS="${PAIRS:-15}"
OPS="${OPS:-200000}"
CC_BIN="${CC:-cc}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

mkdir -p "$WORK" "$RESULTS"
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

skip_case() {
    log "SKIP: $1"
    python3 - "$RESULTS/result.json" "$1" <<'PY'
import json, sys
out_path, reason = sys.argv[1], sys.argv[2]
payload = {"success": True, "skipped": True, "skipped_or_success": True,
           "skip_reason": reason,
           "correctness": 0, "interfaces": {}}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, ensure_ascii=False)
print(json.dumps(payload, indent=2, ensure_ascii=False))
PY
    exit 0
}

# ------------------------------------------------------ locate the source
SRC="${LIBEVENT_SRC:-}"
if [ -z "$SRC" ] && [ -f /workspace/libevent/CMakeLists.txt ]; then
    SRC=/workspace/libevent
fi
[ -n "$SRC" ] && [ -f "$SRC/CMakeLists.txt" ] || skip_case "LIBEVENT_SRC is not set to a libevent source tree"
if grep -q 'evbuffer_chain_new_reference' "$SRC/buffer.c" 2>/dev/null; then
    skip_case "source already contains the recipe output (not a pristine tree)"
fi

log "source: $SRC"

# ----------------------------------------------------------- copy + patch
rm -rf "$WORK/base" "$WORK/cand"
mkdir -p "$WORK/base" "$WORK/cand"
cp -a "$SRC/." "$WORK/base/" 2>/dev/null
cp -a "$SRC/." "$WORK/cand/" 2>/dev/null
rm -rf "$WORK/base/build" "$WORK/cand/build"

for patch_file in "$CASE_DIR"/recipes/*.patch; do
    if ! patch -p1 -d "$WORK/cand" --forward --silent < "$patch_file" >>"$LOG" 2>&1; then
        fail "apply $(basename "$patch_file")"
        echo '{"success": false, "skipped": false, "error": "patch failed"}' > "$RESULTS/result.json"
        exit 1
    fi
    log "applied $(basename "$patch_file")"
done

# ---------------------------------------------------------------- build
build_tree() {
    local dir="$1"
    if command -v ninja >/dev/null 2>&1; then
        cmake -S "$dir" -B "$dir/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
            -DEVENT__DISABLE_OPENSSL=ON -DEVENT__DISABLE_BENCHMARK=ON >>"$LOG" 2>&1 || return 1
    else
        cmake -S "$dir" -B "$dir/build" -DCMAKE_BUILD_TYPE=Release \
            -DEVENT__DISABLE_OPENSSL=ON -DEVENT__DISABLE_BENCHMARK=ON >>"$LOG" 2>&1 || return 1
    fi
    cmake --build "$dir/build" -j "$JOBS" --target event_core_static regress >>"$LOG" 2>&1 \
        || cmake --build "$dir/build" -j "$JOBS" >>"$LOG" 2>&1 || return 1
    find "$dir/build" -name 'libevent_core.a' | head -1
}

BASE_LIB="$(build_tree "$WORK/base")" || fail "baseline build (see $LOG)"
CAND_LIB="$(build_tree "$WORK/cand")" || fail "candidate build (see $LOG)"
if [ -z "${BASE_LIB:-}" ] || [ -z "${CAND_LIB:-}" ]; then
    echo '{"success": false, "error": "build"}' > "$RESULTS/result.json"
    exit 1
fi
log "built: $BASE_LIB"
log "built: $CAND_LIB"

# ------------------------------------------------------- bench binaries
PROBE="$SKILL_DIR/scripts/alloc_probe.c"
[ -f "$PROBE" ] || PROBE="$CASE_DIR/scripts/alloc_probe.c"

compile_bench() {
    local dir="$1" lib="$2" out="$3" extra="${4:-}"
    # shellcheck disable=SC2086
    "$CC_BIN" -O2 -I "$dir/include" -I "$dir/build/include" \
        "$CASE_DIR/bench/bench.c" $extra \
        "$lib" -lpthread -o "$out" >>"$LOG" 2>&1
}
compile_bench "$WORK/base" "$BASE_LIB" "$WORK/bench_base" || fail "bench baseline"
compile_bench "$WORK/cand" "$CAND_LIB" "$WORK/bench_cand" || fail "bench candidate"
if [ -f "$PROBE" ]; then
    compile_bench "$WORK/base" "$BASE_LIB" "$WORK/bench_base_alloc" \
        "$PROBE -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free" \
        || fail "alloc bench baseline"
    compile_bench "$WORK/cand" "$CAND_LIB" "$WORK/bench_cand_alloc" \
        "$PROBE -Wl,--wrap=malloc -Wl,--wrap=calloc -Wl,--wrap=realloc -Wl,--wrap=free" \
        || fail "alloc bench candidate"
else
    fail "allocation probe not found (expected at $PROBE)"
fi

# ---------------------------------------------------------- correctness
REGRESS="$WORK/cand/build/bin/regress"
if [ -x "$REGRESS" ] && "$REGRESS" --quiet 'evbuffer/..' >>"$LOG" 2>&1; then
    log "correctness: evbuffer regression group passed"
    CORRECT=1
else
    fail "evbuffer regression group"
    CORRECT=0
fi

# ------------------------------------------------------------- timing
# drain dominates the allocator path; readline builds reference chains too.
# Speedups are the median of the per-pair ratios (not the ratio of medians),
# so a slow drift that hits both sides of a pair cancels out.
declare -A BASE_VALS CAND_VALS
for mode in drain readline append; do
    case "$mode" in
        append) mode_ops=$((OPS * 10)) ;;
        readline) mode_ops=$((OPS * 3)) ;;
        *) mode_ops="$OPS" ;;
    esac
    base_vals=(); cand_vals=()
    for _ in $(seq "$PAIRS"); do
        base_vals+=("$("$WORK/bench_base" "$mode" "$mode_ops" 2>/dev/null | sed -n 's/.*ns_per_op=//p')")
        cand_vals+=("$("$WORK/bench_cand" "$mode" "$mode_ops" 2>/dev/null | sed -n 's/.*ns_per_op=//p')")
    done
    BASE_VALS[$mode]="${base_vals[*]}"
    CAND_VALS[$mode]="${cand_vals[*]}"
done

# ------------------------------------------------- allocation footprint
BYTES_RATIO=0
if [ -x "$WORK/bench_base_alloc" ] && [ -x "$WORK/bench_cand_alloc" ]; then
    BASE_BYTES="$("$WORK/bench_base_alloc" drain "$OPS" 2>&1 >/dev/null | sed -n 's/.*alloc_report bytes=\([0-9]*\).*/\1/p' | tail -1)"
    CAND_BYTES="$("$WORK/bench_cand_alloc" drain "$OPS" 2>&1 >/dev/null | sed -n 's/.*alloc_report bytes=\([0-9]*\).*/\1/p' | tail -1)"
    if [ -n "${BASE_BYTES:-}" ] && [ -n "${CAND_BYTES:-}" ] && [ "${CAND_BYTES:-0}" -gt 0 ]; then
        BYTES_RATIO="$(python3 -c "print($BASE_BYTES / $CAND_BYTES)")"
        log "drain allocation bytes: baseline=$BASE_BYTES candidate=$CAND_BYTES ratio=$(printf '%.2f' "$BYTES_RATIO")"
    else
        fail "allocation accounting produced no data"
    fi
fi

for mode in drain readline append; do
    if [ -z "${BASE_VALS[$mode]:-}" ] || [ -z "${CAND_VALS[$mode]:-}" ]; then
        fail "timing produced no samples for $mode"
    fi
done
if [ "$FAILED" -ne 0 ] || [ "$BYTES_RATIO" = "0" ]; then
    python3 - "$RESULTS/result.json" <<'PY'
import json, sys
payload = {"success": False, "skipped": False,
           "error": "timing or allocation accounting produced no data; see run.log"}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, ensure_ascii=False)
print(json.dumps(payload, indent=2, ensure_ascii=False))
PY
    exit 1
fi

# ------------------------------------------------------------- verdict
python3 - "$RESULTS/result.json" "$CORRECT" "$BYTES_RATIO" "$FAILED" \
    "${BASE_VALS[drain]}" "${CAND_VALS[drain]}" \
    "${BASE_VALS[readline]}" "${CAND_VALS[readline]}" \
    "${BASE_VALS[append]}" "${CAND_VALS[append]}" <<'PY'
import json, statistics, sys

(out_path, correct, ratio, failed,
 b_drain, c_drain, b_read, c_read, b_app, c_app) = sys.argv[1:]

ratio = float(ratio or 0)

def summarise(base_str, cand_str):
    base = [float(x) for x in base_str.split()]
    cand = [float(x) for x in cand_str.split()]
    if not base or not cand or len(base) != len(cand):
        return None
    ratios = [b / c for b, c in zip(base, cand) if c]
    return {
        "baseline_median_ns": statistics.median(base),
        "candidate_median_ns": statistics.median(cand),
        "pairs": len(ratios),
        "speedup": statistics.median(ratios),
        "speedup_min": min(ratios),
        "speedup_max": max(ratios),
    }

interfaces = {}
for name, base_str, cand_str in (("drain", b_drain, c_drain), ("readline", b_read, c_read), ("append", b_app, c_app)):
    summary = summarise(base_str, cand_str)
    if summary:
        interfaces[name] = summary

improved = sum(1 for v in interfaces.values() if v["speedup"] >= 1.05)
gates = {
    "correctness": int(correct) == 1,
    "allocation_bytes_reduction_gte_8x": ratio >= 8.0,
    "drain_speedup_gte_2x": interfaces["drain"]["speedup"] >= 2.0,
    "readline_no_regression": interfaces["readline"]["speedup"] >= 0.95,
    "append_no_regression": interfaces["append"]["speedup"] >= 0.90,
}
result = {
    "success": all(gates.values()) and int(failed) == 0,
    "skipped": False,
    "skipped_or_success": all(gates.values()) and int(failed) == 0,
    "correctness": int(correct),
    "allocation_bytes_reduction": ratio,
    "drain_speedup": interfaces["drain"]["speedup"],
    "interfaces_improved": improved,
    "interfaces": interfaces,
    "gates": gates,
    "all_gates_passed": all(gates.values()),
}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
print(json.dumps(result, indent=2, ensure_ascii=False))
PY

python3 - "$RESULTS/result.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for name, value in data.get("interfaces", {}).items():
    print(f"[case] {name}: median {value['baseline_median_ns']:.2f} -> "
          f"{value['candidate_median_ns']:.2f} ns/op, speedup {value['speedup']:.3f} "
          f"(pair range {value['speedup_min']:.3f}..{value['speedup_max']:.3f})")
PY

[ "$FAILED" -ne 0 ] && exit 1
python3 -c "import json,sys; d=json.load(open('$RESULTS/result.json')); sys.exit(0 if d['success'] else 1)"
