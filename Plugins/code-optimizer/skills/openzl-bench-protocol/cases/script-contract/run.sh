#!/bin/bash
# Tier-A case runner: drive the three skill scripts with mocks and unit tests,
# then write results/result.json. Exit 0 only if every group is failure-free.
#
# Groups:
#   prepare_kvdata  python unit tests (torch stubbed; numpy parts optional)
#   bench_kv        bench_kv.sh against a mock unitBench + mock data files
#   td_loop         td_loop.sh argument/parsing contract against the same mock
#   static          bash -n syntax checks + P2 regression guards (no .tolist(),
#                   torch.load pinned to weights_only, no legacy slice names)
#
# Usage: bash cases/script-contract/run.sh   (from anywhere)

set -u
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$CASE_DIR/../.." && pwd)"
RESULTS_DIR="$CASE_DIR/results"
mkdir -p "$RESULTS_DIR"
TMP="$(mktemp -d)"
# guard with BASH_SUBSHELL so a dying pipeline/command-substitution subshell
# (which inherits this trap) cannot delete the main shell's temp dir
trap '[ "${BASH_SUBSHELL:-0}" -eq 0 ] && rm -rf "$TMP"' EXIT

BENCH_KV="$SKILL_DIR/scripts/bench_kv.sh"
TD_LOOP="$SKILL_DIR/scripts/td_loop.sh"
PREPARE="$SKILL_DIR/scripts/prepare_kvdata.py"
TEST_PY="$CASE_DIR/test_prepare_kvdata.py"

declare -i KV_RAN=0 KV_FAIL=0 BK_RAN=0 BK_FAIL=0 TD_RAN=0 TD_FAIL=0
declare -i STATIC_RAN=0 STATIC_FAIL=0
NUMPY_AVAILABLE=false

log() { echo "[case] $*"; }

# check <group> <desc> <rc>  — group in {static,bk,td}; records ran/fail
check() {
    local group="$1" desc="$2" rc="$3"
    case "$group" in
        static) STATIC_RAN+=1; [ "$rc" -ne 0 ] && STATIC_FAIL+=1 ;;
        bk)     BK_RAN+=1;     [ "$rc" -ne 0 ] && BK_FAIL+=1 ;;
        td)     TD_RAN+=1;     [ "$rc" -ne 0 ] && TD_FAIL+=1 ;;
    esac
    if [ "$rc" -ne 0 ]; then log "FAIL: $desc"; else log "ok: $desc"; fi
}

# ---------------- static checks ----------------
bash -n "$BENCH_KV";  check static "bench_kv.sh syntax" $?
bash -n "$TD_LOOP";   check static "td_loop.sh syntax" $?
if grep -q "\.tolist()" "$PREPARE" || grep -q "Counter(" "$PREPARE"; then
    check static "prepare_kvdata.py must not materialize python objects (P2)" 1
else
    check static "prepare_kvdata.py must not materialize python objects (P2)" 0
fi
if grep -n "torch\.load(" "$PREPARE" | grep -qv "weights_only=True"; then
    check static "prepare_kvdata.py torch.load pinned to weights_only=True (P2)" 1
else
    check static "prepare_kvdata.py torch.load pinned to weights_only=True (P2)" 0
fi
if grep -Eq "qwen_kv_|(bf16|e4m3|e5m2)_[0-9]+MB" "$BENCH_KV" "$TD_LOOP"; then
    check static "bench_kv.sh/td_loop.sh free of legacy slice filenames (P2)" 1
else
    check static "bench_kv.sh/td_loop.sh free of legacy slice filenames (P2)" 0
fi

# ---------------- prepare_kvdata python tests ----------------
PY_OUT="$(python3 "$TEST_PY" 2>&1)"
PY_RC=$?
KV_RAN=$(echo "$PY_OUT" | sed -n 's/^PYTEST_SUMMARY:.*"ran": \([0-9]*\).*/\1/p')
KV_SUMMARY="$(echo "$PY_OUT" | grep '^PYTEST_SUMMARY:' | head -1 | cut -d: -f2-)"
if [ -z "$KV_RAN" ]; then KV_RAN=0; PY_RC=1; fi
echo "$PY_OUT" | grep -v '^PYTEST_SUMMARY:'
KV_FAIL=$(( PY_RC != 0 ? 1 : 0 ))
log "prepare_kvdata: ran=$KV_RAN rc=$PY_RC"
echo "$KV_SUMMARY" | grep -q '"numpy": true' && NUMPY_AVAILABLE=true

# ---------------- mock unitBench + mock data ----------------
MOCK_UB="$TMP/unitBench"
cat > "$MOCK_UB" <<'EOF'
#!/bin/bash
# mock unitBench (tier-A): canned output lines matching real unitBench format
if [ "${1:-}" = "--list" ]; then
    echo "${MOCK_SCENARIOS:-kvBf16Encode,kvBf16Decode,kvFp8Encode,kvFp8Decode,kvE5M2Encode,kvE5M2Decode}"
    exit 0
fi
SCEN=""
for a in "$@"; do
    case "$a" in kv*) SCEN="$a" ;; esac
done
[ -n "$SCEN" ] || { echo "mock: no scenario arg" >&2; exit 1; }
case "$SCEN" in
    *Encode) echo "process mockdata (91356 B) with $SCEN in 10 ms ==> 123.4 MB/s (45678)" ;;
    *Decode) echo "decode mockdata (x1.13) with $SCEN into 44606 KB (x1.13) in 10 ms ==> 234.5 MB/s" ;;
esac
EOF
chmod +x "$MOCK_UB"

MOCK_DATA="$TMP/kvdata"
mkdir -p "$MOCK_DATA"
# e4m3 data only; 91356 bytes so ratio = 91356/45678 = 2.000
dd if=/dev/zero of="$MOCK_DATA/qwen_test_kv_e4m3.bin" bs=91356 count=1 2>/dev/null

# slice-contract data: full (91356 B -> R=2.000) plus an 8MB slice named per
# prepare_kvdata.py convention (45678 B -> R=1.000, tells the two files apart)
MOCK_DATA_SLICE="$TMP/kvdata-slice"
mkdir -p "$MOCK_DATA_SLICE"
dd if=/dev/zero of="$MOCK_DATA_SLICE/qwen_test_kv_e4m3.bin" bs=91356 count=1 2>/dev/null
dd if=/dev/zero of="$MOCK_DATA_SLICE/qwen_test_kv_e4m3_8MB.bin" bs=45678 count=1 2>/dev/null

# ---------------- bench_kv tests ----------------
"$BENCH_KV" -h >/dev/null 2>&1; check bk "bench_kv.sh --help exits 0" $?

OUT="$("$BENCH_KV" -d "$TMP/no-such-dir" -u "$MOCK_UB" 2>&1)"; RC=$?
if [ $RC -eq 1 ] && echo "$OUT" | grep -q "SKIP bf16: missing data"; then
    check bk "all data missing: exit 1 with SKIP lines" 0
else
    check bk "all data missing: exit 1 with SKIP lines" 1
fi

OUT="$("$BENCH_KV" -f e4m3 -d "$MOCK_DATA" -u "$MOCK_UB" 2>&1)"; RC=$?
if [ $RC -eq 0 ] \
   && echo "$OUT" | grep -q "kvFp8Encode.*123\.4.*45678.*2\.000" \
   && echo "$OUT" | grep -q "kvFp8Decode.*1\.13.*234\.5"; then
    check bk "e4m3 happy path: parsed MB/s, outbytes and ratios" 0
else
    check bk "e4m3 happy path: parsed MB/s, outbytes and ratios" 1
fi

OUT="$(MOCK_SCENARIOS=kvFp8Encode "$BENCH_KV" -f e4m3 -d "$MOCK_DATA" -u "$MOCK_UB" 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "SKIP kvFp8Decode: scenario not registered" \
   && echo "$OUT" | grep -q "kvFp8Encode"; then
    check bk "unregistered scenario skipped, registered one still runs" 0
else
    check bk "unregistered scenario skipped, registered one still runs" 1
fi

OUT="$("$BENCH_KV" -f e4m3 -s 8MB -d "$MOCK_DATA_SLICE" -u "$MOCK_UB" 2>&1)"; RC=$?
# slice file is 45678 B and mock outbytes=45678 -> R=1.000 (full file gives 2.000)
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "kvFp8Encode.*123\.4.*45678.*1\.000"; then
    check bk "8MB slice resolved as <stem>_8MB.bin (prepare_kvdata naming)" 0
else
    check bk "8MB slice resolved as <stem>_8MB.bin (prepare_kvdata naming)" 1
fi

# ---------------- td_loop tests ----------------
"$TD_LOOP" --help >/dev/null 2>&1; check td "td_loop.sh --help exits 0" $?

"$TD_LOOP" >/dev/null 2>&1; RC=$?
check td "missing --scenario exits non-zero" $([ $RC -ne 0 ] && echo 0 || echo 1)

UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario compress --format invalid \
    >/dev/null 2>&1; RC=$?
check td "invalid --format exits non-zero" $([ $RC -ne 0 ] && echo 0 || echo 1)

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 --duration 1 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "scenario=compress bench=kvFp8Encode.*throughput=123\.4 MB/s ratio=2\.000"; then
    check td "compress: throughput and computed ratio parsed" 0
else
    check td "compress: throughput and computed ratio parsed" 1
fi

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario decompress --format e4m3 --cpu 0 --duration 1 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "scenario=decompress bench=kvFp8Decode.*throughput=234\.5 MB/s ratio=1\.13"; then
    check td "decompress: throughput and stream ratio parsed" 0
else
    check td "decompress: throughput and stream ratio parsed" 1
fi

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 --rounds 3 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "rounds=3"; then
    check td "explicit rounds mode reports rounds=3" 0
else
    check td "explicit rounds mode reports rounds=3" 1
fi

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA_SLICE" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 --slice 8MB 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "data=$MOCK_DATA_SLICE/qwen_test_kv_e4m3_8MB.bin" \
   && echo "$OUT" | grep -q "ratio=1\.000"; then
    check td "--slice 8MB resolves <stem>_8MB.bin (prepare_kvdata naming)" 0
else
    check td "--slice 8MB resolves <stem>_8MB.bin (prepare_kvdata naming)" 1
fi

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA_SLICE" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "qwen_test_kv_e4m3_8MB.bin"; then
    check td "default slice is 8MB (aligned with prepare_kvdata --slice-mb)" 0
else
    check td "default slice is 8MB (aligned with prepare_kvdata --slice-mb)" 1
fi

OUT="$(UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 --slice 128MB 2>&1)"; RC=$?
if [ $RC -eq 0 ] && echo "$OUT" | grep -q "WARN" \
   && echo "$OUT" | grep -q "data=$MOCK_DATA/qwen_test_kv_e4m3.bin"; then
    check td "missing slice falls back to full with WARN (not silent)" 0
else
    check td "missing slice falls back to full with WARN (not silent)" 1
fi

UB="$MOCK_UB" KVDATA_DIR="$MOCK_DATA" "$TD_LOOP" --scenario compress --format e4m3 --cpu 0 --slice bogus \
    >/dev/null 2>&1; RC=$?
check td "invalid --slice exits non-zero" $([ $RC -ne 0 ] && echo 0 || echo 1)

# ---------------- result.json ----------------
SUCCESS=false
if [ "$KV_FAIL" -eq 0 ] && [ "$BK_FAIL" -eq 0 ] && [ "$TD_FAIL" -eq 0 ] \
   && [ "$STATIC_FAIL" -eq 0 ] && [ "$KV_RAN" -gt 0 ]; then
    SUCCESS=true
fi

python3 - "$RESULTS_DIR/result.json" "$SUCCESS" "$KV_RAN" "$KV_FAIL" \
    "$BK_RAN" "$BK_FAIL" "$TD_RAN" "$TD_FAIL" "$STATIC_RAN" "$STATIC_FAIL" \
    "$NUMPY_AVAILABLE" "$KV_SUMMARY" <<'EOF'
import json
import sys

(out_path, success, kv_ran, kv_fail, bk_ran, bk_fail, td_ran, td_fail,
 static_ran, static_fail, numpy_available, kv_summary) = sys.argv[1:]
result = {
    "success": success == "true",
    "prepare_kvdata_ran": int(kv_ran),
    "prepare_kvdata_failures": int(kv_fail),
    "bench_kv_ran": int(bk_ran),
    "bench_kv_failures": int(bk_fail),
    "td_loop_ran": int(td_ran),
    "td_loop_failures": int(td_fail),
    "static_ran": int(static_ran),
    "static_failures": int(static_fail),
    "numpy_available": numpy_available == "true",
}
if kv_summary:
    try:
        result["prepare_kvdata_detail"] = json.loads(kv_summary)
    except ValueError:
        pass
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
EOF

log "result: $RESULTS_DIR/result.json"
if [ "$SUCCESS" = true ]; then
    exit 0
fi
exit 1
