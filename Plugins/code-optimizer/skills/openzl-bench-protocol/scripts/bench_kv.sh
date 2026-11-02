#!/bin/bash
# OpenZL unitBench KV-cache scenario benchmark: encode/decode throughput +
# compression ratio across formats (BF16 / FP8 E4M3 / FP8 E5M2) and entropy
# modes. Skips formats whose data file or scenario is unavailable.
#
# Adapted from OpenZL unitBench helper scripts (BSD-3-Clause).
# Data preparation: scripts/prepare_kvdata.py in this skill.
#
# Usage:
#   ./bench_kv.sh [options]
# Options:
#   -f <list>   formats: bf16,e4m3,e5m2 or "all" (default all)
#   -p <mode>   entropy mode 0..3 (0=Huffman-token, 1=FSE, 2=auto, 3=multi)
#   -i <secs>   unitBench duration per scenario (default 2)
#   -c <cpus>   taskset CPU list, e.g. "0-191" or "$(seq -s, 0 2 383)"
#   -d <dir>    kv data dir (default ${KVDATA_DIR:-$HOME/kvdata})
#   -s <slice>  data size: full | 8MB (default full)
#   -u <path>   unitBench binary (default ${UB:-./unitBench})
#   -h          help
#
# Output: markdown-ish table (stdout), raw lines in $TMPDIR/kv_bench_raw.txt

set -u

FORMATS="all"
MODE=0
DUR=2
CPUS=""
DATA_DIR="${KVDATA_DIR:-$HOME/kvdata}"
SLICE="full"
UB="${UB:-./unitBench}"

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while getopts "f:p:i:c:d:s:u:h" opt; do
    case "$opt" in
        f) FORMATS="$OPTARG" ;;
        p) MODE="$OPTARG" ;;
        i) DUR="$OPTARG" ;;
        c) CPUS="$OPTARG" ;;
        d) DATA_DIR="$OPTARG" ;;
        s) SLICE="$OPTARG" ;;
        u) UB="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

[ -x "$UB" ] || { echo "ERROR: unitBench not found at $UB (build: make unitBench)"; exit 1; }
[ "$SLICE" = "full" ] || [ "$SLICE" = "8MB" ] || { echo "ERROR: -s must be full|8MB"; exit 1; }
[ "$FORMATS" = "all" ] && FORMATS="bf16,e4m3,e5m2"
IFS=',' read -r -a FMT_LIST <<< "$FORMATS"

# Naming contract (shared with td_loop.sh / prepare_kvdata.py, see SKILL.md):
# <stem>.bin (full) and <stem>_<N>MB.bin (slice), where stem is the input .pt
# basename without extension, e.g. qwen_test_kv_e4m3.pt -> qwen_test_kv_e4m3*.
stem_of() { # $1=fmt
    case "$1" in
        bf16) echo "qwen_test_kv" ;;
        e4m3) echo "qwen_test_kv_e4m3" ;;
        e5m2) echo "qwen_test_kv_e5m2" ;;
    esac
}
file_of() { # $1=fmt $2=slice (full | <N>MB)
    local stem
    stem="$(stem_of "$1")"
    if [ "$2" = "full" ]; then
        echo "$stem.bin"
    else
        echo "${stem}_$2.bin"
    fi
}
scen_prefix() { # bf16->Bf16, e4m3->Fp8, e5m2->E5M2
    case "$1" in
        bf16) echo "kvBf16" ;;
        e4m3) echo "kvFp8" ;;
        e5m2) echo "kvE5M2" ;;
    esac
}

RUNNER=""
[ -n "$CPUS" ] && RUNNER="taskset -c $CPUS"

registered() { "$UB" --list 2>&1 | tr ',' '\n' | grep -q "$1"; }

parse_stats() { # $1=scenario  $2=file -> echoes "enc=<MB/s> out=<B> Renc=<R> dec=<MB/s> Rdec=<R>"
    local scen="$1" file="$2"
    local enc_line dec_line enc_mbs outbytes srcbytes enc_R dec_mbs dec_R
    # encode line: "process <file> (...) with <scen> in .. ms ==> MB/s (outbytes)"
    enc_line=$($RUNNER "$UB" -i=$DUR -p=$MODE "$scen" "$file" 2>/dev/null \
        | tr '\r' '\n' | grep -E "^process .*with $scen" | head -1)
    enc_mbs=$(echo "$enc_line" | sed -n 's/.*==> \([0-9.]*\) MB\/s.*/\1/p')
    outbytes=$(echo "$enc_line" | sed -n 's/.*(\([0-9]*\))[[:space:]]*$/\1/p')
    srcbytes=$(stat -c %s "$file" 2>/dev/null)
    if [ -n "$srcbytes" ] && [ -n "$outbytes" ] && [ "$outbytes" -gt 0 ] 2>/dev/null; then
        enc_R=$(awk "BEGIN{printf \"%.3f\", $srcbytes/$outbytes}")
    fi
    # decode line: "decode <file> (...) with <scen> into .. KB (xR) in .. ms ==> MB/s"
    dec_line=$($RUNNER "$UB" -i=$DUR -p=$MODE "$scen" "$file" 2>/dev/null \
        | tr '\r' '\n' | grep -E "^decode" | head -1)
    dec_mbs=$(echo "$dec_line" | sed -n 's/.*==> \([0-9.]*\) MB\/s.*/\1/p')
    dec_R=$(echo "$dec_line" | sed -n 's/.*(x\([0-9.]*\)).*/\1/p')
    echo "enc=${enc_mbs:-NA} out=${outbytes:-NA} Renc=${enc_R:-NA} dec=${dec_mbs:-NA} Rdec=${dec_R:-NA}"
}

echo "== OpenZL KV benchmark | -p=$MODE -i=$DUR slice=$SLICE =="
echo "== data: $DATA_DIR | unitBench: $UB | $(date) =="
RAW="${TMPDIR:-/tmp}/kv_bench_raw.txt"
: > "$RAW"

printf '%-16s %-12s %-14s %-8s %-12s\n' "scenario" "encode MB/s" "out bytes" "R" "decode MB/s"
printf '%s\n' "---------------- -------------- ------------ -------- ------------"
RAN=0
for fmt in "${FMT_LIST[@]}"; do
    prefix=$(scen_prefix "$fmt")
    file="$DATA_DIR/$(file_of "$fmt" "$SLICE")"
    if [ ! -f "$file" ]; then
        echo "## SKIP $fmt: missing data $file (prepare via prepare_kvdata.py)" | tee -a "$RAW"
        continue
    fi
    for s in "${prefix}Encode" "${prefix}Decode"; do
        if ! registered "$s"; then
            echo "## SKIP $s: scenario not registered in unitBench" | tee -a "$RAW"
            continue
        fi
        echo "##### $s @ $file" >> "$RAW"
        res=$(parse_stats "$s" "$file")
        echo "$s $res" >> "$RAW"
        enc_mbs=$(echo "$res" | sed -n 's/^enc=\([0-9.]*\).*/\1/p')
        outbytes=$(echo "$res" | sed -n 's/.* out=\([0-9]*\).*/\1/p')
        Renc=$(echo "$res" | sed -n 's/.* Renc=\([0-9.]*\).*/\1/p')
        dec_mbs=$(echo "$res" | sed -n 's/.* dec=\([0-9.]*\).*/\1/p')
        Rdec=$(echo "$res" | sed -n 's/.* Rdec=\([0-9.]*\).*/\1/p')
        case "$s" in
            *Encode) Rval="$Renc" ;;
            *)       Rval="$Rdec" ;;
        esac
        printf '%-16s %-12s %-14s %-8s %-12s\n' \
            "$s" "$enc_mbs" "$outbytes" "$Rval" "$dec_mbs"
        RAN=$((RAN + 1))
    done
done
echo
[ "$RAN" -gt 0 ] || { echo "ERROR: nothing ran (all formats skipped)"; exit 1; }
echo "raw log: $RAW"
