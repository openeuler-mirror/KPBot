#!/bin/bash
# py-spy collect — Python 进程性能采集统一入口（record/top/dump）
set -euo pipefail

MODE="record"
PID=""
DURATION=30
RATE=""
FORMAT="flamegraph"
OUT=""
SWITCHES=()

print_help() {
    cat <<'EOF'
kptools-py-spy collect — Collect Python process performance data

Usage:
  collect.sh -p <PID> [--record|--top|--dump] [options]

Modes:
  --record (default)  Sample stacks to a flamegraph/raw/speedscope/chrometrace file
  --top               Live top view (TTY)；非 TTY 自动降级为 raw 统计排序
  --dump              One-shot thread stack dump to stdout

Options:
  -p <PID>            Target Python process（必填，--help 除外）
  -d <sec>            Sampling duration（record/top 降级模式，默认 30；dump 忽略）
  -o <file>           Output file（默认 ./py-spy-evidence/<时间戳>/）
  --format <fmt>      flamegraph | raw | speedscope | chrometrace（默认 flamegraph）
  -r, --rate <Hz>     Sampling rate（默认 100；与 java-perf -i ms 语义方向相反）
  -s, --subprocesses  Include subprocesses（torchrun 多 rank）
  -g, --gil           Only threads holding the GIL（GIL 竞争分析）
  -i, --idle          Include idle threads（off-CPU 近似）
  -n, --native        Include native extension stacks（实验性：需 debug symbols，torch_npu 待实测）
  -t, --threads       Show thread ids in the output
  --help              Show this help

Notes:
  stdout: 输出文件路径（record）或线程栈文本（top 降级/dump）
  stderr: py-spy 诊断 + 权限/降级提示
  exit 0: 成功；2: 依赖缺失；3: 参数错误；1: 采集失败
EOF
    exit 0
}

# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -p|-d|-o|-r|--rate|--format) echo "ERROR: option ${!#} requires a value" >&2; exit 3 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="$2"; shift 2 ;;
        -d) DURATION="$2"; shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        -r|--rate) RATE="$2"; shift 2 ;;
        --format) FORMAT="$2"; shift 2 ;;
        --record) MODE="record"; shift ;;
        --top) MODE="top"; shift ;;
        --dump) MODE="dump"; shift ;;
        -s|--subprocesses) SWITCHES+=("--subprocesses"); shift ;;
        -g|--gil) SWITCHES+=("--gil"); shift ;;
        -i|--idle) SWITCHES+=("--idle"); shift ;;
        -n|--native) SWITCHES+=("--native"); shift ;;
        -t|--threads) SWITCHES+=("--threads"); shift ;;
        --help) print_help ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 3 ;;
    esac
done

if [ -z "$PID" ]; then
    echo "ERROR: -p <PID> is required" >&2
    exit 3
fi

if ! [[ "$PID" =~ ^[0-9]+$ ]]; then
    echo "ERROR: -p 必须为数字 PID" >&2
    exit 3
fi

if ! command -v py-spy &>/dev/null; then
    echo "ERROR: py-spy not found. Install: pip install py-spy 或运行 bash scripts/deploy.sh" >&2
    exit 2
fi

case "$FORMAT" in
    flamegraph) EXT="svg" ;;
    raw) EXT="txt" ;;
    speedscope|chrometrace) EXT="json" ;;
    *) echo "ERROR: --format 仅支持 flamegraph|raw|speedscope|chrometrace" >&2; exit 3 ;;
esac

for v in "$DURATION" "$RATE"; do
    [ -z "$v" ] && continue
    if ! [[ "$v" =~ ^[0-9]+$ ]] || [ "$v" -lt 1 ]; then
        echo "ERROR: -d/-r 必须为正整数" >&2
        exit 3
    fi
done

# ptrace 预检：受限且非 root 时提前提示（仍尝试采集，失败信息更明确）
YAMA="/proc/sys/kernel/yama/ptrace_scope"
if [ -r "$YAMA" ] && [ "$(cat "$YAMA")" != "0" ] && [ "$(id -u)" != "0" ]; then
    echo "NOTICE: ptrace_scope=$(cat "$YAMA") 且非 root，采集可能失败（root 或 sysctl kernel.yama.ptrace_scope=0 可解除）" >&2
fi

for sw in "${SWITCHES[@]:-}"; do
    if [ "$sw" = "--native" ]; then
        echo "NOTE: -n native 为实验性能力（需 Python debug symbols；torch_npu 下效果待实测）" >&2
    fi
done

# 翻译 py-spy 常见失败为明确提示
translate_error() {
    local err="$1"
    if [[ "$err" == *"Failed to find python version"* ]]; then
        echo "ERROR: 目标进程（pid ${PID}）不是 Python 进程；请先用主文档 §3.5 路由判定语言（Java→kptools-devkit java-perf，Node.js→kptools-node-profiler）" >&2
    elif [[ "$err" == *"Operation not permitted"* || "$err" == *"Permission"* ]]; then
        echo "ERROR: 无 ptrace 权限（pid ${PID}）。需 root、同 uid 且 ptrace_scope=0，容器内需 --cap-add=SYS_PTRACE" >&2
    else
        printf '%s\n' "$err" >&2
    fi
}

run_pyspy() {
    local err
    set +e
    err=$(py-spy "$@" 2>&1 >/dev/null)
    PYSPY_RC=$?
    set -e
    if [ "$PYSPY_RC" -ne 0 ]; then
        translate_error "$err"
    fi
}

if [ "$MODE" = "dump" ]; then
    ERR_FILE=$(mktemp /tmp/pyspy_dump.XXXXXX.err)
    set +e
    py-spy dump -p "$PID" 2>"$ERR_FILE"
    PYSPY_RC=$?
    set -e
    if [ -s "$ERR_FILE" ]; then
        translate_error "$(cat "$ERR_FILE")"
    fi
    rm -f "$ERR_FILE"
    exit "$PYSPY_RC"
fi

if [ "$MODE" = "top" ]; then
    if [ -t 1 ]; then
        exec py-spy top -p "$PID"
    fi
    echo "NOTICE: stdout 非 TTY，py-spy top 交互视图不可用，降级为 raw 统计按采样数排序（时长 ${DURATION}s）" >&2
    TMP_RAW=$(mktemp /tmp/pyspy_top.XXXXXX.txt)
    run_pyspy record -p "$PID" -d "$DURATION" -f raw -o "$TMP_RAW"
    RC="$PYSPY_RC"
    if [ "$RC" -ne 0 ]; then
        rm -f "$TMP_RAW"
        exit "$RC"
    fi
    # raw 行格式 "栈帧 (file:line) N"：按末列采样数降序
    awk '{n=$NF; sub(/[ \t]+[0-9]+$/, ""); printf "%s\t%s\n", n, $0}' "$TMP_RAW" \
        | sort -t"$(printf '\t')" -k1,1 -rn \
        | awk -F'\t' '{printf "%s  %s\n", $2, $1}'
    rm -f "$TMP_RAW"
    exit 0
fi

# record 模式
if [ -n "$OUT" ]; then
    mkdir -p "$(dirname "$OUT")"
    OUT_FILE="$OUT"
else
    OUT_DIR="./py-spy-evidence/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$OUT_DIR"
    OUT_FILE="$OUT_DIR/py_spy_${PID}_$(date +%H%M%S).${EXT}"
fi

ARGS=(-p "$PID" -d "$DURATION" -f "$FORMAT" -o "$OUT_FILE")
if [ -n "$RATE" ]; then
    ARGS+=(-r "$RATE")
fi
if [ "${#SWITCHES[@]}" -gt 0 ]; then
    ARGS+=("${SWITCHES[@]}")
fi

run_pyspy record "${ARGS[@]}"
if [ "$PYSPY_RC" -ne 0 ]; then
    exit "$PYSPY_RC"
fi
echo "$OUT_FILE"
