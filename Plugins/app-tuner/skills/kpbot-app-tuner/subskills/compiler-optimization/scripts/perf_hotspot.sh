#!/bin/bash
# ARM64应用函数热点分析脚本
# 用法: perf_hotspot.sh <pid> [duration] [output_dir]

set -euo pipefail

ARCH=$(uname -m)
if [ "$ARCH" != "aarch64" ]; then
    echo "Error: This script requires an ARM64/aarch64 system. Current architecture: $ARCH"
    exit 1
fi

PID="${1:-}"
DURATION=${2:-5}
OUTPUT_DIR=${3:-.}
OUTPUT_FILE="${OUTPUT_DIR}/perf_hotspot_${PID}.txt"
PERF_DATA="${OUTPUT_DIR}/perf_${PID}.data"

cleanup() {
    rm -f "$PERF_DATA"
    echo "Interrupted, cleaned up perf data."
}
trap cleanup SIGINT SIGTERM

if [ -z "$PID" ]; then
    echo "Usage: $0 <pid> [duration] [output_dir]"
    echo "  pid       - 目标进程PID"
    echo "  duration  - 采集时长(秒), 默认5"
    echo "  output_dir - 输出目录, 默认当前目录"
    exit 1
fi

if ! command -v perf &>/dev/null; then
    echo "Error: perf not found. Install with: sudo yum install perf || sudo apt install linux-perf"
    exit 1
fi

if [ ! -d "/proc/$PID" ]; then
    echo "Error: Process $PID not found"
    exit 1
fi

if ! [[ "$DURATION" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: duration must be a positive integer, got: $DURATION"
    exit 1
fi

mkdir -p "$OUTPUT_DIR"
rm -f "$PERF_DATA"

if ! perf record -p "$PID" -g -o "$PERF_DATA" -- sleep "$DURATION" 2>/dev/null; then
    echo "Error: perf record failed. Possible causes:"
    echo "  1. Permission denied - try: sudo sysctl -w kernel.perf_event_paranoid=1"
    echo "  2. Process exited during profiling"
    rm -f "$PERF_DATA"
    exit 1
fi

if ! LC_ALL=C perf report -i "$PERF_DATA" --stdio --no-children --max-stack 0 2>/dev/null | grep -E "^ +[0-9]" | head -n 50 > "$OUTPUT_FILE"; then
    echo "Error: perf report failed"
    rm -f "$PERF_DATA"
    exit 1
fi

LINE_COUNT=$(wc -l < "$OUTPUT_FILE")
echo "Hotspot profile saved to $OUTPUT_FILE ($LINE_COUNT functions)"
rm -f "$PERF_DATA"
