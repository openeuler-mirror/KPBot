#!/bin/bash
# Thread-CPU balance check — rule identical to app-tuner check_cpu_balance.sh
# (max-min >= 2 -> skewed; single cpu -> single_cpu_or_single_thread; empty -> ps_thread_view_empty)
set -euo pipefail

print_help() {
    cat <<'EOF'
check_balance — Thread-CPU balance analysis from ps thread view

Usage:
  check_balance.sh -p <PID>             read threads via ps -L
  check_balance.sh -f <ps_output_file>  analyze a saved ps -L output (psr column)

Output (stdout): JSON with balance_status, hot_cpu_list, thread_distribution,
max/min_threads_on_cpu. exit 0 on any completed analysis (including degraded).
EOF
    exit 0
}

PID=""
FILE=""
# 值参数不能作为最后一个参数出现（缺值时报清晰错误而非 unbound variable）
if [ $# -gt 0 ]; then
    case "${!#}" in
        -p|-f) echo "ERROR: option ${!#} requires a value" >&2; exit 1 ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        -p) PID="$2"; shift 2 ;;
        -f) FILE="$2"; shift 2 ;;
        --help) print_help ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [ -n "$FILE" ]; then
    if [ ! -r "$FILE" ]; then
        echo "ERROR: cannot read $FILE" >&2
        exit 1
    fi
    # 取数值型的 psr：兼容带表头三列（PID TID PSR）、无表头三列、单列（ps -o psr=）三种存档
    PSR_LINES=$(awk '$3 ~ /^-?[0-9]+$/ {print $3; next} NF == 1 && $1 ~ /^-?[0-9]+$/ {print $1}' "$FILE")
elif [ -n "$PID" ]; then
    if ! command -v ps &>/dev/null; then
        echo "ERROR: ps not found" >&2
        exit 1
    fi
    if ! PSR_LINES=$(ps -L -p "$PID" -o psr= 2>/dev/null); then
        PSR_LINES=""
    fi
else
    echo "ERROR: -p <PID> or -f <file> required" >&2
    exit 1
fi

if [ -z "$(echo "$PSR_LINES" | tr -d '[:space:]')" ]; then
    echo '{"balance_status": "ps_thread_view_empty", "hot_cpu_list": [], "thread_distribution": {}}'
    exit 0
fi

DIST=$(echo "$PSR_LINES" | sort -n | uniq -c | awk '{a[NR]=$2; c[NR]=$1} END {s=""; for(i=1;i<=NR;i++){s = s "\"" a[i] "\": " c[i]; if(i<NR) s=s ", "} print s}')

MAX=$(echo "$PSR_LINES" | sort -n | uniq -c | sort -rn | head -1 | awk '{print $1}')
MIN=$(echo "$PSR_LINES" | sort -n | uniq -c | sort -n | head -1 | awk '{print $1}')
MAX_CPUS=$(echo "$PSR_LINES" | sort -n | uniq -c | awk -v m="$MAX" '$1==m {print $2}' | sort -n | tr '\n' ',' | sed 's/,$//')
NCPU=$(echo "$PSR_LINES" | sort -n | uniq | wc -l)

HOT_JSON=$(echo "$MAX_CPUS" | awk '{gsub(/,/, "\", \""); print "[\"" $0 "\"]"}')

if [ "$NCPU" -le 1 ]; then
    STATUS="single_cpu_or_single_thread"
elif [ $((MAX - MIN)) -ge 2 ]; then
    STATUS="skewed"
else
    STATUS="balanced"
fi

echo "{\"balance_status\": \"$STATUS\", \"hot_cpu_list\": $HOT_JSON, \"thread_distribution\": {$DIST}, \"max_threads_on_cpu\": $MAX, \"min_threads_on_cpu\": $MIN}"
exit 0
