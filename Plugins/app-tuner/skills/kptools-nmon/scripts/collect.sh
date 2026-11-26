#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DURATION=10
INTERVAL=1
SNAPSHOT=false
TOP_MODE="none"
JSON_OUTPUT=false
KEEP_DIR=""

print_help() {
    cat <<'EOF'
nmon collect — Collect system performance data (CPU/memory/disk IO/network)

Usage:
  collect.sh [options]

Options:
  -d <sec>      Collection duration in seconds (default: 10)
  -i <sec>      Sampling interval in seconds (default: 1)
  --top         Include Top Processes stats (nmon -t)
  --top-args    Include Top Processes stats with command arguments (nmon -T, implies --top)
  --snapshot    Single snapshot mode (equivalent to -d 1 -i 1)
  --json        Wrap output in a JSON envelope (data file is kept, path reported)
  -m <dir>      Keep the nmon data file in the given directory (default: temp file, removed after output)
  --help        Show this help

Examples:
  collect.sh -d 30 -i 1
  collect.sh --snapshot
  collect.sh -d 30 --top
  collect.sh -d 30 --json

Notes:
  stdout: nmon CSV raw data (or JSON envelope with --json)
  stderr: diagnostics (data file path, conversion notices, errors)
  exit 0: success; non-zero: failure
EOF
}

# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -d|-i|-m) echo "ERROR: option ${!#} requires a value" >&2; exit 1 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -d) DURATION="$2"; shift 2 ;;
        -i) INTERVAL="$2"; shift 2 ;;
        --top) TOP_MODE="top"; shift ;;
        --top-args) TOP_MODE="top_args"; shift ;;
        --snapshot) SNAPSHOT=true; shift ;;
        --json) JSON_OUTPUT=true; shift ;;
        -m) KEEP_DIR="$2"; shift 2 ;;
        --help) print_help; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; print_help >&2; exit 1 ;;
    esac
done

if ! command -v nmon &>/dev/null; then
    echo "ERROR: nmon not found. Install: yum install nmon or apt install nmon" >&2
    exit 1
fi

if [ "$JSON_OUTPUT" = true ] && ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found (required for --json; install python3 or drop --json)" >&2
    exit 1
fi

if [ "$SNAPSHOT" = true ]; then
    DURATION=1
    INTERVAL=1
fi

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || ! [[ "$INTERVAL" =~ ^[0-9]+$ ]]; then
    echo "ERROR: -d and -i must be positive integers" >&2
    exit 1
fi

if [ "$DURATION" -lt 1 ] || [ "$INTERVAL" -lt 1 ]; then
    echo "ERROR: -d and -i must be >= 1" >&2
    exit 1
fi

if [ "$INTERVAL" -gt "$DURATION" ]; then
    echo "ERROR: interval ($INTERVAL s) must not exceed duration ($DURATION s)" >&2
    exit 1
fi

COUNT=$(( (DURATION + INTERVAL - 1) / INTERVAL ))
ACTUAL=$(( COUNT * INTERVAL ))
if [ "$ACTUAL" -ne "$DURATION" ]; then
    echo "NOTICE: duration $DURATION s is not divisible by interval $INTERVAL s; actual collection: $ACTUAL s ($COUNT snapshots)" >&2
fi

TOP_ARGS=()
case "$TOP_MODE" in
    top) TOP_ARGS=(-t) ;;
    top_args) TOP_ARGS=(-T) ;;
esac

if [ -n "$KEEP_DIR" ]; then
    mkdir -p "$KEEP_DIR"
    OUT_DIR="$KEEP_DIR"
else
    OUT_DIR=$(mktemp -d /tmp/nmon-collect.XXXXXX)
fi

# --json 时数据文件路径要写进 envelope，必须保留；其余场景脚本退出时清理临时目录
KEEP_DATA=false
if [ "$JSON_OUTPUT" = true ]; then
    KEEP_DATA=true
fi

cleanup() {
    if [ -z "${KEEP_DIR:-}" ] && [ "$KEEP_DATA" = false ] && [ -n "${OUT_DIR:-}" ]; then
        rm -rf "$OUT_DIR"
    fi
}
trap cleanup EXIT

# nmon -f/-F forks to background and returns immediately; -p prints the
# background PID so we can wait for the data file to be fully written.
DATA_FILE="$OUT_DIR/nmon_$(date +%Y%m%d_%H%M%S)_$$.nmon"

NMON_RAW=$(nmon -p -F "$(basename "$DATA_FILE")" -s "$INTERVAL" -c "$COUNT" "${TOP_ARGS[@]}" -m "$OUT_DIR") || {
    echo "ERROR: nmon exited with an error" >&2
    exit 1
}

# -p 输出必须是纯 PID；若混入其他文本（版本横幅等），tail --pid 会失败或挂死
NMON_PID=$(printf '%s\n' "$NMON_RAW" | grep -xE '[0-9]+' | head -1)
if [ -z "$NMON_PID" ]; then
    echo "ERROR: could not parse background PID from nmon -p output: $NMON_RAW" >&2
    exit 1
fi

# 等待策略：nmon 每完成一个快照就向数据文件写入一行 ZZZZ 时间戳，达到 COUNT
# 即数据完整。不强依赖进程退出：部分环境 BBBP 元数据采集很慢（数千接口的容器
# 宿主机上 ifconfig 可达数十秒），个别 nmon 16p 构建在快照完成后进程也不退出。
WAIT_DEADLINE=$((SECONDS + ACTUAL + 300))
while kill -0 "$NMON_PID" 2>/dev/null; do
    SNAPSHOTS=$(grep -c '^ZZZZ' "$DATA_FILE" 2>/dev/null) || true
    if [ "${SNAPSHOTS:-0}" -ge "$COUNT" ]; then
        timeout 5 tail --pid="$NMON_PID" -f /dev/null 2>/dev/null || kill "$NMON_PID" 2>/dev/null || true
        break
    fi
    if [ "$SECONDS" -ge "$WAIT_DEADLINE" ]; then
        kill "$NMON_PID" 2>/dev/null || true
        if [ "${SNAPSHOTS:-0}" -eq 0 ]; then
            echo "ERROR: nmon background process (pid $NMON_PID) produced no snapshot within ${WAIT_DEADLINE}s; aborted" >&2
            exit 1
        fi
        echo "WARNING: wait budget ${WAIT_DEADLINE}s exhausted with ${SNAPSHOTS}/${COUNT} snapshots; terminated nmon (pid $NMON_PID) and proceeding with partial data" >&2
        break
    fi
    sleep 1
done

if [ ! -s "$DATA_FILE" ]; then
    echo "ERROR: nmon data file not found or empty: $DATA_FILE" >&2
    exit 1
fi

echo "nmon data file: $DATA_FILE" >&2

if [ "$JSON_OUTPUT" = true ]; then
    python3 - "$DATA_FILE" "$ACTUAL" "$INTERVAL" "$SNAPSHOT" "$TOP_MODE" <<'PYEOF'
import json, sys
data_file, duration, interval, snapshot, top = sys.argv[1:6]
with open(data_file, "r", errors="replace") as f:
    data = f.read()
print(json.dumps({
    "skill": "kptools-nmon",
    "mode": "snapshot" if snapshot == "true" else "duration",
    "duration_sec": int(duration),
    "interval_sec": int(interval),
    "snapshot": snapshot == "true",
    "top_processes": top != "none",
    "data_file": data_file,
    "data": data,
}, ensure_ascii=False))
PYEOF
else
    cat "$DATA_FILE"
fi
