#!/bin/bash
# OpenZL topdown/TMA 采集专用热点负载：持续循环跑 KV 压缩/解压（默认 20s），
# 供 topdown 采集工具包裹（perf stat -M / 厂商 TMA 脚本 -c 模式）。
#
# 实现说明：unitBench 的 -i=<secs> 语义即"该文件持续测试 N 秒"（内部循环迭代
# 直至时长耗尽），等价于 while 循环热点负载，且无进程重启开销。
# --rounds 模式额外提供显式多轮（每轮一个进程）的语义，用于统计稳定性。
#
# Adapted from OpenZL unitBench helper scripts (BSD-3-Clause).
#
# Usage（参数风格对齐通用 topdown 负载约定）:
#   ./td_loop.sh --scenario compress --cpu 4 --duration 20
#   ./td_loop.sh --scenario decompress --cpu 4 --duration 20 --format e4m3
#   ./td_loop.sh --scenario compress --cpu 4 --rounds 10            # 10 轮 × 1s
#
# 配合 topdown 采集（-s/-e 需与 --cpu 一致）：
#   # AMD Zen（perf 原生 AMD TMA metric）：
#   taskset -c 4 perf stat -M frontend_bound,backend_bound,bad_speculation,retiring \
#        ./td_loop.sh --cpu 4 --duration 20 --scenario decompress
#   # Kunpeng（鲲鹏 TMA 脚本 -c 模式，脚本路径按环境替换）：
#   bash <path-to>/perf-cpu-topdown.sh -s 4 -e 4 \
#        -c "./td_loop.sh --cpu 4 --duration 20 --scenario compress"

set -u

SCENARIO=""
CPU=4
DURATION=20
ROUNDS=0
FORMAT="e5m2"
MODE=0
DATA=""
SLICE="8MB"   # 与 prepare_kvdata.py 默认 --slice-mb 8 对齐
KVDATA_DIR="${KVDATA_DIR:-$HOME/kvdata}"
UB="${UB:-./unitBench}"

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --scenario) SCENARIO="$2"; shift 2 ;;
        --cpu)      CPU="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --rounds)   ROUNDS="$2"; shift 2 ;;
        --format)   FORMAT="$2"; shift 2 ;;
        --mode)     MODE="$2"; shift 2 ;;
        --data)     DATA="$2"; shift 2 ;;
        --slice)    SLICE="$2"; shift 2 ;;
        -h|--help)  usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -n "$SCENARIO" ] || { echo "ERROR: --scenario compress|decompress required" >&2; exit 1; }
case "$SCENARIO" in
    compress)   ;;
    decompress) ;;
    *) echo "ERROR: invalid scenario '$SCENARIO'" >&2; exit 1 ;;
esac

# ---- resolve scenario & data (与 bench_kv.sh 同款约定) ----
# 文件名契约与 prepare_kvdata.py 产出一致（见 SKILL.md 数据准备节）：
# 全量 <stem>.bin，切片 <stem>_<N>MB.bin，stem = 输入 .pt 文件名去扩展名
case "$FORMAT" in
    bf16)
        case "$SCENARIO" in
            compress)   SCEN=kvBf16Encode ;;
            decompress) SCEN=kvBf16Decode ;;
        esac
        STEM=qwen_test_kv
        ;;
    e4m3)
        case "$SCENARIO" in
            compress)   SCEN=kvFp8Encode ;;
            decompress) SCEN=kvFp8Decode ;;
        esac
        STEM=qwen_test_kv_e4m3
        ;;
    e5m2)
        case "$SCENARIO" in
            compress)   SCEN=kvE5M2Encode ;;
            decompress) SCEN=kvE5M2Decode ;;
        esac
        STEM=qwen_test_kv_e5m2
        ;;
    *) echo "ERROR: invalid format '$FORMAT' (bf16|e4m3|e5m2)" >&2; exit 1 ;;
esac

if [ "$SLICE" != "full" ] && ! [[ "$SLICE" =~ ^[0-9]+MB$ ]]; then
    echo "ERROR: invalid --slice '$SLICE' (full | <N>MB)" >&2; exit 1
fi

if [ -z "$DATA" ]; then
    if [ "$SLICE" = "full" ]; then
        DATA="$KVDATA_DIR/$STEM.bin"
    else
        DATA="$KVDATA_DIR/${STEM}_${SLICE}.bin"
        if [ ! -f "$DATA" ]; then   # 切片缺失回退全量：显式 WARN，避免误以为在测切片
            echo "WARN: slice missing: $DATA; falling back to full $STEM.bin" >&2
            DATA="$KVDATA_DIR/$STEM.bin"
        fi
    fi
fi
[ -f "$DATA" ] || { echo "ERROR: data not found: $DATA (prepare via prepare_kvdata.py)" >&2; exit 1; }
[ -x "$UB" ]    || { echo "ERROR: unitBench not found at $UB (build: make unitBench)" >&2; exit 1; }
"$UB" --list 2>&1 | tr ',' '\n' | grep -q "$SCEN" || { echo "ERROR: scenario $SCEN not registered" >&2; exit 1; }

RUNNER="taskset -c $CPU"

# ---- run hotspot loop ----
# 默认：单进程 -i=<duration>（unitBench 内部循环至时长耗尽，零重启开销）
# --rounds N：显式 N 轮，每轮 -i=1s（对齐 rounds 语义，供多轮统计）
run_once() {  # $1 = per-run duration
    $RUNNER "$UB" -i="$1" -p="$MODE" "$SCEN" "$DATA" 2>/dev/null \
        | tr '\r' '\n' | grep -E "^(process .*with $SCEN|^decode)" | head -1
}

t0=$(date +%s.%N)
if [ "$ROUNDS" -gt 0 ]; then
    for r in $(seq 1 "$ROUNDS"); do
        LAST=$(run_once 1)
    done
    ROUNDS_DONE=$ROUNDS
else
    LAST=$(run_once "$DURATION")
    ROUNDS_DONE=1
fi
t1=$(date +%s.%N)

# ---- parse throughput & ratio from the last run line ----
TP=$(echo "$LAST" | sed -n 's/.*==> \([0-9.]*\) MB\/s.*/\1/p')
RATIO=$(echo "$LAST" | sed -n 's/.*(x\([0-9.]*\)).*/\1/p')   # decode 行: (x1.12)
if [ -z "$RATIO" ]; then                                      # compress 行: (outbytes) → 计算 R
    OUTB=$(echo "$LAST" | sed -n 's/.*(\([0-9]*\))[[:space:]]*$/\1/p')
    SRCB=$(stat -c %s "$DATA")
    if [ -n "$OUTB" ] && [ "$OUTB" -gt 0 ] 2>/dev/null; then
        RATIO=$(awk -v s="$SRCB" -v o="$OUTB" 'BEGIN{printf "%.3f", s/o}')
    fi
fi
SIZE_MB=$(awk -v s="$(stat -c %s "$DATA")" 'BEGIN{printf "%.0f", s/1e6}')
ELAPSED=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')

echo "td_loop done: scenario=$SCENARIO bench=$SCEN cpu=$CPU format=$FORMAT mode=$MODE \
rounds=$ROUNDS_DONE size=${SIZE_MB}MB elapsed=${ELAPSED}s \
throughput=${TP:-NA} MB/s ratio=${RATIO:-NA} data=$DATA"
