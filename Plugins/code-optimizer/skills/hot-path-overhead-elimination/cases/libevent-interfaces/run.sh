#!/bin/bash
# Tier-B case runner: apply this skill's recipes to a real libevent checkout and
# verify the outcome on three evbuffer interfaces.
#
# Requires a pristine libevent 2.1.12 source tree in LIBEVENT_SRC (this case
# cannot ship the library itself).  When the variable is unset or the tree does
# not look pristine, the case records skipped=true and exits 0 — it is a guard,
# not a silent pass: the log says exactly why nothing was verified.
#
# What it does:
#   1. copies the source twice: baseline and candidate;
#   2. applies recipes/0001-hot-path-overhead-elimination.patch to the candidate;
#   3. builds both and runs the evbuffer regression group on the candidate;
#   4. compares append / readline / drain with alternating pairs and medians;
#   5. checks that the requested allocation bytes are unchanged — this skill
#      must win through call-path overhead, not through the allocator.
#
# Usage: LIBEVENT_SRC=/path/to/libevent-2.1.12 bash run.sh
set -u

CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$CASE_DIR/work"
RESULTS="$CASE_DIR/results"
LOG="$WORK/run.log"
REPS="${REPS:-7}"
# 15 pairs: the regression guards on readline/drain need a stable median of
# per-pair ratios, and 7 pairs is too few on a shared host.
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

write_result() {
    python3 - "$RESULTS/result.json" "$@" <<'PY'
import json, sys
out_path = sys.argv[1]
payload = json.loads(sys.argv[2])
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, ensure_ascii=False)
print(json.dumps(payload, indent=2, ensure_ascii=False))
PY
}

skip_case() {
    log "SKIP: $1"
    write_result "$(python3 -c '
import json, sys
print(json.dumps({
  "success": True,
  "skipped": True,
  "skipped_or_success": True,
  "skip_reason": sys.argv[1],
  "correctness": 0,
  "interfaces": {},
}, ensure_ascii=False))
' "$1")"
    exit 0
}

# ------------------------------------------------------ locate the source
SRC="${LIBEVENT_SRC:-}"
if [ -z "$SRC" ] && [ -f /workspace/libevent/CMakeLists.txt ]; then
    SRC=/workspace/libevent
fi
[ -n "$SRC" ] && [ -f "$SRC/CMakeLists.txt" ] || skip_case "LIBEVENT_SRC is not set to a libevent source tree"
grep -q 'release-2.1.12' "$SRC/ChangeLog" 2>/dev/null || log "note: ChangeLog does not mention 2.1.12; patches may not apply"
# This case's own payload symbols.  The allocation-recipe symbol belongs to a
# different skill and must not be used as the pristine-tree test here.
if grep -q 'evbuffer_invoke_callbacks_slow_' "$SRC/buffer.c" 2>/dev/null; then
    skip_case "source already contains this case's patch output (not a pristine tree)"
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
        write_result '{"success": false, "skipped": false, "error": "patch failed"}'
        exit 1
    fi
    log "applied $(basename "$patch_file")"
done

# ---------------------------------------------------------------- build
build_tree() {
    local dir="$1" name="$2"
    if command -v ninja >/dev/null 2>&1; then
        cmake -S "$dir" -B "$dir/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
            -DEVENT__DISABLE_OPENSSL=ON -DEVENT__DISABLE_BENCHMARK=ON \
            >>"$LOG" 2>&1 || return 1
    else
        cmake -S "$dir" -B "$dir/build" -DCMAKE_BUILD_TYPE=Release \
            -DEVENT__DISABLE_OPENSSL=ON -DEVENT__DISABLE_BENCHMARK=ON \
            >>"$LOG" 2>&1 || return 1
    fi
    cmake --build "$dir/build" -j "$JOBS" --target event_core_static regress >>"$LOG" 2>&1 \
        || cmake --build "$dir/build" -j "$JOBS" >>"$LOG" 2>&1 || return 1
    local lib
    lib="$(find "$dir/build" -name 'libevent_core.a' | head -1)"
    [ -n "$lib" ] || return 1
    echo "$lib"
}

BASE_LIB="$(build_tree "$WORK/base" baseline)" || { fail "baseline build (see $LOG)"; }
CAND_LIB="$(build_tree "$WORK/cand" candidate)" || { fail "candidate build (see $LOG)"; }
[ -n "${BASE_LIB:-}" ] && [ -n "${CAND_LIB:-}" ] || { write_result '{"success": false, "error": "build"}'; exit 1; }
log "built: $BASE_LIB"
log "built: $CAND_LIB"

# ------------------------------------------------------- bench binaries
compile_bench() {
    local dir="$1" lib="$2" out="$3" extra="${4:-}"
    # shellcheck disable=SC2086
    "$CC_BIN" -O2 -I "$dir/include" -I "$dir/build/include" \
        "$CASE_DIR/bench/bench.c" $extra \
        "$lib" -lpthread -o "$out" >>"$LOG" 2>&1
}
compile_bench "$WORK/base" "$BASE_LIB" "$WORK/bench_base" || { fail "bench baseline"; }
compile_bench "$WORK/cand" "$CAND_LIB" "$WORK/bench_cand" || { fail "bench candidate"; }

# --------------------------------------------------------- decoupling guard
# This skill must not buy its win from the allocator.  The allocation recipe
# rewrites reference-chain allocation around evbuffer_chain_new_reference, so
# the mere presence of that symbol in the patch means the two skills got mixed.
ALLOC_FREE=0
if grep -q 'evbuffer_chain_new_reference' "$CASE_DIR"/recipes/*.patch; then
    fail "patch touches the allocation recipe (evbuffer_chain_new_reference)"
else
    ALLOC_FREE=1
    log "decoupling: patch contains no allocation-path recipe"
fi

# ---------------------------------------------------------- correctness
REGRESS="$WORK/cand/build/bin/regress"
if [ -x "$REGRESS" ]; then
    if "$REGRESS" --quiet 'evbuffer/..' >>"$LOG" 2>&1; then
        log "correctness: evbuffer regression group passed"
        CORRECT=1
    else
        fail "evbuffer regression group"
        CORRECT=0
    fi
else
    fail "regress binary not built"
    CORRECT=0
fi

# ------------------------------------------------------------- timing
# Short interfaces need longer runs: a 15 ns/op loop over 200k ops lasts only
# a few milliseconds, which is dominated by scheduling noise on a shared box.
# Speedups are the median of the per-pair ratios (not the ratio of medians), so
# a slow drift that hits both sides of a pair cancels out.
declare -A BASE_VALS CAND_VALS
for mode in append readline drain; do
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

for mode in append readline drain; do
    if [ -z "${BASE_VALS[$mode]:-}" ] || [ -z "${CAND_VALS[$mode]:-}" ]; then
        fail "timing produced no samples for $mode"
    fi
done
if [ "$FAILED" -ne 0 ]; then
    write_result '{"success": false, "skipped": false, "error": "timing produced no samples; see run.log"}'
    exit 1
fi

# ------------------------------------------------------------- verdict
python3 - "$RESULTS/result.json" "$CORRECT" "$ALLOC_FREE" "$FAILED" \
    "${BASE_VALS[append]}" "${CAND_VALS[append]}" \
    "${BASE_VALS[readline]}" "${CAND_VALS[readline]}" \
    "${BASE_VALS[drain]}" "${CAND_VALS[drain]}" <<'PY'
import json, statistics, sys

(out_path, correct, alloc_free, failed,
 b_app, c_app, b_read, c_read, b_drain, c_drain) = sys.argv[1:]

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
for name, base_str, cand_str in (("append", b_app, c_app), ("readline", b_read, c_read), ("drain", b_drain, c_drain)):
    summary = summarise(base_str, cand_str)
    if summary:
        interfaces[name] = summary

improved = sum(1 for v in interfaces.values() if v["speedup"] >= 1.05)
gates = {
    "correctness": int(correct) == 1,
    "append_speedup_gte_1_10x": interfaces["append"]["speedup"] >= 1.10,
    "readline_no_regression": interfaces["readline"]["speedup"] >= 0.95,
    "drain_no_regression": interfaces["drain"]["speedup"] >= 0.90,
    # This skill must not buy its win from the allocator: the patch must not
    # touch the allocation recipe at all (that is the other skill's job).
    "patch_free_of_allocation_recipe": int(alloc_free) == 1,
}
result = {
    "success": all(gates.values()) and int(failed) == 0,
    "skipped": False,
    "skipped_or_success": all(gates.values()) and int(failed) == 0,
    "correctness": int(correct),
    "append_speedup": interfaces["append"]["speedup"],
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
