#!/bin/bash
# 在线进程采样 orchestrator
# 用法: ./sample_online_pid.sh <PID>
# 输出: 仅打印生成的 JSON 报告文件的绝对路径（与 run_and_profile_offline.sh 同构）
#
# 设计要点：
# 1. 目标进程已在外部运行，本脚本只读采样，不启动也不 kill 目标进程。
# 2. 静态库依赖（lsof + /proc/<PID>/maps）总是采集并写入整合 JSON，
#    作为 perf 失败/无权限时的兜底——修复"perf 不可用时静态检测结果丢失"的问题。
# 3. perf 为 best-effort：权限不足时在 JSON 中标记 needs_authorization，不中断流程。
# 4. 输出格式与离线脚本完全一致，detect_all_libraries.sh 可无差别处理。

set -euo pipefail

PID=$1
if [[ -z "$PID" ]]; then
    echo '{"error": "PID is required"}'
    exit 1
fi

REPORT_FILE="/tmp/library_replacement_report_online_${PID}_$$.json"
DATA_FILE="/tmp/perf_online_${PID}_$$.data"
LIBS_TMP="/tmp/libs_online_${PID}_$$.txt"

trap 'rm -f "$DATA_FILE" "$LIBS_TMP"' EXIT

# -------------------------------------------------------------------------
# 0. 进程存在性检查
# -------------------------------------------------------------------------
if ! kill -0 "$PID" 2>/dev/null; then
    cat > "$REPORT_FILE" <<EOF
{"error": "process $PID not found", "process_found": false, "libraries": [], "perf_available": false, "hotspots": []}
EOF
    echo "$REPORT_FILE"
    exit 0
fi

# -------------------------------------------------------------------------
# 1. 静态采集：ps + lsof + /proc/<PID>/maps（总是执行，作为 perf 失败时的兜底）
# -------------------------------------------------------------------------
PS_OUTPUT=$(ps -p "$PID" -o pid=,%cpu=,%mem=,vsz=,rss=,comm= --no-headers 2>/dev/null || echo "")

: > "$LIBS_TMP"
# lsof：最后一列为库路径
lsof -p "$PID" 2>/dev/null | grep '\.so' | awk '{print $NF}' >> "$LIBS_TMP" || true
# /proc/<PID>/maps：第 6 列为映射路径（lsof 缺失或权限不足时覆盖更全）
grep '\.so' "/proc/$PID/maps" 2>/dev/null | awk '{print $6}' >> "$LIBS_TMP" || true
LSOF_OUTPUT=$(sort -u "$LIBS_TMP" 2>/dev/null || echo "")

# -------------------------------------------------------------------------
# 2. perf 采样（best-effort，处理权限不足）
# -------------------------------------------------------------------------
PERF_AVAILABLE="false"
PERF_ERROR=""
NEEDS_AUTH="false"
AUTH_COMMAND=""
HOTSPOTS_JSON="[]"

if ! command -v perf &> /dev/null; then
    PERF_ERROR="perf command not found, please install: apt install linux-tools-generic"
elif [[ ! -r "/proc/$PID" ]]; then
    PERF_ERROR="Cannot access /proc/$PID, permission denied"
    NEEDS_AUTH="true"
    AUTH_COMMAND="sudo chmod 755 /proc/$PID"
else
    PERF_WINDOW="${PERF_WINDOW:-10}"
    PERF_OUT=$(timeout $((PERF_WINDOW + 5)) perf record -p "$PID" -g --call-graph dwarf -o "$DATA_FILE" -- sleep "$PERF_WINDOW" 2>&1 || true)
    if echo "$PERF_OUT" | grep -qi "Permission denied"; then
        PERF_ERROR="perf requires higher privilege to sample process $PID (check kernel.perf_event_paranoid)"
        NEEDS_AUTH="true"
        AUTH_COMMAND="sudo sysctl -w kernel.perf_event_paranoid=1"
    elif [[ -f "$DATA_FILE" ]]; then
        PERF_AVAILABLE="true"
        HOTSPOTS_JSON=$(perf report --stdio -i "$DATA_FILE" -F overhead,dso,symbol --no-call-graph 2>/dev/null | \
        awk '
            BEGIN { print "["; first=1; count=0 }
            /^#/ || NF<3 { next }
            {
              if (count >= 30) next
              overhead = $1; dso = $2; symbol = $3
              for(i=4; i<=NF; i++) symbol = symbol " " $i
              gsub(/\\/, "\\\\", symbol); gsub(/"/, "\\\"", symbol)
              gsub(/\\/, "\\\\", dso); gsub(/"/, "\\\"", dso)
              if (!first) print ","
              printf "    {\"overhead\": \"%s\", \"lib\": \"%s\", \"symbol\": \"%s\"}", overhead, dso, symbol
              first = 0; count++
            }
            END { print "\n  ]" }
        ' 2>/dev/null || echo "[]")
    else
        PERF_ERROR="perf sampling failed: $PERF_OUT"
    fi
fi

# -------------------------------------------------------------------------
# 3. 组装整合 JSON（与 run_and_profile_offline.sh 同构）
# -------------------------------------------------------------------------
LIBS_JSON="["
first=1
while IFS= read -r lib; do
    [[ -z "$lib" ]] && continue
    lib_escaped="${lib//\\/\\\\}"
    lib_escaped="${lib_escaped//\"/\\\"}"
    if [[ $first -eq 1 ]]; then first=0; else LIBS_JSON+=", "; fi
    LIBS_JSON+="\"${lib_escaped}\""
done <<< "$LSOF_OUTPUT"
LIBS_JSON+="]"

if [[ -n "$PS_OUTPUT" ]]; then
    read -r pid_val cpu_val mem_val vsz_val rss_val comm_val <<< "$PS_OUTPUT"
    pid_val="${pid_val:-0}"
    comm_val="${comm_val:-unknown}"
    PS_INFO_JSON="\"ps_info\": {\"pid\": $pid_val, \"comm\": \"$comm_val\", \"cpu\": \"${cpu_val:-0}\", \"mem\": \"${mem_val:-0}\", \"vsz\": \"${vsz_val:-0}\", \"rss\": \"${rss_val:-0}\"}"
else
    PS_INFO_JSON="\"ps_info\": null"
fi

PERF_EXTRA=""
if [[ -n "$PERF_ERROR" ]]; then
    PERF_ERROR_ESC="${PERF_ERROR//\\/\\\\}"
    PERF_ERROR_ESC="${PERF_ERROR_ESC//\"/\\\"}"
    PERF_ERROR_ESC="${PERF_ERROR_ESC//$'\n'/\\n}"
    PERF_ERROR_ESC="${PERF_ERROR_ESC//$'\r'/\\r}"
    PERF_ERROR_ESC="${PERF_ERROR_ESC//$'\t'/\\t}"
    PERF_EXTRA=", \"perf_error\": \"$PERF_ERROR_ESC\""
fi
if [[ "$NEEDS_AUTH" == "true" ]]; then
    PERF_EXTRA+=", \"needs_authorization\": true, \"auth_command\": \"$AUTH_COMMAND\""
fi

cat > "$REPORT_FILE" <<EOF
{
  "pid": $PID,
  "process_found": true,
  $PS_INFO_JSON,
  "libraries": $LIBS_JSON,
  "perf_available": $PERF_AVAILABLE,
  "hotspots": $HOTSPOTS_JSON$PERF_EXTRA
}
EOF

# 唯一标准输出：绝对路径
echo "$REPORT_FILE"
