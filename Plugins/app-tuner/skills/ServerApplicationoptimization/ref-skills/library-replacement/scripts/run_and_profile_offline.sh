#!/bin/bash
# 离线闭环启动与性能采样脚本
# 用法: ./run_and_profile_offline.sh <command> [args...]
# 输出: 仅打印生成的 JSON 报告文件的绝对路径

set -euo pipefail

COMMAND="$*"
REPORT_FILE="/tmp/library_replacement_report_$$.json"

if [[ -z "$COMMAND" ]]; then
  echo "{\"error\": \"no command provided\"}" > "$REPORT_FILE"
  echo "$REPORT_FILE"
  exit 1
fi

DATA_FILE="/tmp/perf_$$.data"

# 确保脚本退出时清理临时采样文件，但不清理生成的报告文件
trap 'rm -f "$DATA_FILE"' EXIT

PS_OUTPUT=""
LSOF_OUTPUT=""
PROCESS_FOUND=0

# -------------------------------------------------------------------------
# 1. 启动 Perf 并包裹目标命令 (采集最多 10 秒)
# -------------------------------------------------------------------------
if command -v perf &> /dev/null; then
  timeout 10 perf record -g -o "$DATA_FILE" -- $COMMAND >/dev/null 2>&1 &
  PERF_PID=$!
else
  # 退化模式：如果没有 perf，仅拉起进程
  eval "$COMMAND" >/dev/null 2>&1 &
  PERF_PID=$!
fi

sleep 0.2

TARGET_PID=$(pgrep -P $PERF_PID | head -n 1)
if [[ -z "$TARGET_PID" ]]; then
  TARGET_PID=$PERF_PID
fi

# -------------------------------------------------------------------------
# 2. 采集静态进程状态
# -------------------------------------------------------------------------
if kill -0 "$TARGET_PID" 2>/dev/null; then
  PROCESS_FOUND=1
  PS_OUTPUT=$(ps -p "$TARGET_PID" -o pid=,%cpu=,%mem=,vsz=,rss=,comm= --no-headers 2>/dev/null || echo "")
  LSOF_OUTPUT=$(lsof -p "$TARGET_PID" 2>/dev/null | grep '\.so' | awk '{print $NF}' | sort -u || echo "")
fi

# 等待采样结束
wait $PERF_PID 2>/dev/null || true

# -------------------------------------------------------------------------
# 3. 解析并拍平 Perf 数据，转换为 JSON 结构
# -------------------------------------------------------------------------
PERF_AVAILABLE="false"
HOTSPOTS_JSON="[]"

if [[ -f "$DATA_FILE" ]] && command -v perf &> /dev/null; then
  PERF_AVAILABLE="true"
  # 最佳实践：使用 -F overhead,dso,symbol 强制输出干净的三列数据，直接交由 awk 封装为 JSON
  HOTSPOTS_JSON=$(perf report --stdio -i "$DATA_FILE" -F overhead,dso,symbol --no-call-graph 2>/dev/null | \
  awk '
    BEGIN { print "[\n"; first=1; count=0 }
    /^#/ || NF<3 { next }
    {
      if (count >= 30) next; # 取 Top 30
      
      overhead = $1;
      dso = $2;
      symbol = $3;
      for(i=4; i<=NF; i++) symbol = symbol " " $i;
      
      gsub(/\\/, "\\\\", symbol);
      gsub(/"/, "\\\"", symbol);
      gsub(/\\/, "\\\\", dso);
      gsub(/"/, "\\\"", dso);
      
      if (!first) print ",";
      printf "    {\"overhead\": \"%s\", \"lib\": \"%s\", \"symbol\": \"%s\"}", overhead, dso, symbol;
      first = 0;
      count++;
    }
    END { print "\n  ]" }
  ')
fi

# -------------------------------------------------------------------------
# 4. JSON 数据组装与落盘
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

if [[ $PROCESS_FOUND -eq 1 && -n "$PS_OUTPUT" ]]; then
  read -r pid_val cpu_val mem_val vsz_val rss_val comm_val <<< "$PS_OUTPUT"
  pid_val="${pid_val:-0}"
  comm_val="${comm_val:-unknown}"
  PS_INFO_JSON="\"ps_info\": {\"pid\": $pid_val, \"comm\": \"$comm_val\", \"cpu\": \"${cpu_val:-0}\", \"mem\": \"${mem_val:-0}\", \"vsz\": \"${vsz_val:-0}\", \"rss\": \"${rss_val:-0}\"}"
else
  PS_INFO_JSON="\"ps_info\": null"
fi

command_escaped="${COMMAND//\\/\\\\}"
command_escaped="${command_escaped//\"/\\\"}"

# 将完整 JSON 写入独立文件
cat << EOF > "$REPORT_FILE"
{
  "command": "$command_escaped",
  "process_found": $( [[ $PROCESS_FOUND -eq 1 ]] && echo "true" || echo "false" ),
  $PS_INFO_JSON,
  "libraries": $LIBS_JSON,
  "perf_available": $PERF_AVAILABLE,
  "hotspots": $HOTSPOTS_JSON
}
EOF

# 清理目标进程
if kill -0 "$TARGET_PID" 2>/dev/null; then
  kill -9 "$TARGET_PID" 2>/dev/null
fi

# 5. 唯一标准输出：绝对路径
echo "$REPORT_FILE"